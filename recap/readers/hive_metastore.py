from pymetastore.htypes import (
    HCharType,
    HDecimalType,
    HListType,
    HMapType,
    HPrimitiveType,
    HStructType,
    HType,
    HUnionType,
    HVarcharType,
    PrimitiveCategory,
)
from pymetastore.metastore import HMS, HColumn

from recap.types import (
    BoolType,
    BytesType,
    FloatType,
    IntType,
    ListType,
    MapType,
    NullType,
    RecapType,
    StringType,
    StructType,
    UnionType,
)


class HiveMetastoreReader:
    def __init__(self, client: HMS):
        self.client = client

    def struct(self, database_name: str, table_name: str) -> StructType:
        table = self.client.get_table(database_name, table_name)
        fields = []
        for col in table.columns:
            fields.append(self._convert(col))
        return StructType(fields=fields, name=table_name)

    def _convert(self, htype: HType | HColumn) -> RecapType:
        recap_type: RecapType | None = None
        extra_attrs = {}
        if isinstance(htype, HColumn):
            extra_attrs["name"] = htype.name
            if htype.comment:
                extra_attrs["comment"] = htype.comment
            htype = htype.type
        match htype:
            case HPrimitiveType(primitive_type=PrimitiveCategory.BOOLEAN):
                recap_type = BoolType()
            case HPrimitiveType(primitive_type=PrimitiveCategory.BYTE):
                recap_type = IntType(bits=8)
            case HPrimitiveType(primitive_type=PrimitiveCategory.SHORT):
                recap_type = IntType(bits=16)
            case HPrimitiveType(primitive_type=PrimitiveCategory.INT):
                recap_type = IntType(bits=32)
            case HPrimitiveType(primitive_type=PrimitiveCategory.LONG):
                recap_type = IntType(bits=64)
            case HPrimitiveType(primitive_type=PrimitiveCategory.FLOAT):
                recap_type = FloatType(bits=32)
            case HPrimitiveType(primitive_type=PrimitiveCategory.DOUBLE):
                recap_type = FloatType(bits=64)
            case HPrimitiveType(primitive_type=PrimitiveCategory.STRING):
                # TODO: Should handle multi-byte encodings
                # Using 2^63-1 as the max length because Hive has no defined max.
                recap_type = StringType(bytes_=9_223_372_036_854_775_807)
            case HPrimitiveType(primitive_type=PrimitiveCategory.BINARY):
                recap_type = BytesType(bytes_=2_147_483_647)
            case HDecimalType(precision=int(precision), scale=int(scale)):
                # TODO: Decimals can be > 38 digits, but need to calculate bytes_.
                if precision > 38:
                    raise ValueError(f"Max decimal precision is 38, got {precision}")
                recap_type = BytesType(
                    logical="build.recap.Decimal",
                    bytes_=16,
                    variable=False,
                    precision=precision,
                    scale=scale,
                )
            case HVarcharType(length=int(length)):
                # TODO: Should handle multi-byte encodings
                recap_type = StringType(bytes_=length)
            case HCharType(length=int(length)):
                # TODO: Should handle multi-byte encodings
                recap_type = StringType(bytes_=length, variable=False)
            case HPrimitiveType(primitive_type=PrimitiveCategory.DATE):
                recap_type = IntType(
                    logical="build.recap.Date",
                    bits=32,
                    signed=True,
                    unit="day",
                    **extra_attrs,
                )
            case HPrimitiveType(primitive_type=PrimitiveCategory.TIMESTAMP):
                # Hive stores timestamps as strings, so 64 bits isn't enough to store
                # all possible values. C'est la vie.
                recap_type = IntType(
                    logical="build.recap.Timestamp",
                    bits=64,
                    signed=True,
                    unit="nanosecond",
                    timezone="UTC",
                    **extra_attrs,
                )
            case HPrimitiveType(primitive_type=PrimitiveCategory.TIMESTAMPLOCALTZ):
                # Hive stores timestamps as strings, so 64 bits isn't enough to store
                # all possible values. C'est la vie.
                recap_type = IntType(
                    logical="build.recap.Timestamp",
                    bits=64,
                    signed=True,
                    unit="nanosecond",
                    # Follow Apache Arrow's Schema.fbs style for local time.
                    # Denote LOCALTZ as a timezone field set to None.
                    timezone=None,
                    **extra_attrs,
                )
            case HPrimitiveType(primitive_type=PrimitiveCategory.INTERVAL_YEAR_MONTH):
                recap_type = BytesType(
                    logical="build.recap.Interval",
                    bytes_=12,
                    signed=True,
                    unit="month",
                    **extra_attrs,
                )
            case HPrimitiveType(primitive_type=PrimitiveCategory.INTERVAL_DAY_TIME):
                recap_type = BytesType(
                    logical="build.recap.Interval",
                    bytes_=12,
                    signed=True,
                    unit="second",
                    **extra_attrs,
                )
            case HMapType(key_type=key_type, value_type=value_type):
                recap_type = MapType(
                    keys=self._convert(key_type),
                    values=self._convert(value_type),
                )
            case HListType(element_type=element_type):
                recap_type = ListType(values=self._convert(element_type))
            case HUnionType(types=types):
                # Hive columns are always nullable and default is always null.
                types = [NullType()] + [self._convert(t) for t in htype.types]
                recap_type = UnionType(types=types, default=None, **extra_attrs)
                recap_type = self._flatten_unions(recap_type)
            case HStructType(names=names, types=types):
                recap_type = StructType(
                    fields=[self._convert(HColumn(n, t)) for n, t in zip(names, types)]
                )
            case _:
                raise ValueError(f"Unsupported type: {htype}")

        if not isinstance(recap_type, UnionType):
            # Hive columns are always nullable and default is always null.
            recap_type = UnionType(
                types=[NullType(), recap_type],
                default=None,
                **extra_attrs,
            )

        return recap_type

    def _flatten_unions(self, recap_type: RecapType) -> RecapType:
        """
        Flattens nested unions into a single union. Useful for unwinding a
        HUnionType since we're wrapping all fields in _convert() in a union.
        Without this, we'd get [NullType, [NullType, IntType], [NullType,
        StrType], ...]. Now we get [NullType, IntType, StrType].
        """

        if isinstance(recap_type, UnionType):
            # Start with empty set of flattened types
            flattened_types = []

            for union_subtype in recap_type.types:
                # We're not expecting string type aliases here.
                assert isinstance(union_subtype, RecapType)

                # Recursively flatten unions within unions
                flattened = self._flatten_unions(union_subtype)

                if isinstance(flattened, UnionType):
                    # Append types from inner union to the flattened types
                    flattened_types.extend(
                        [t for t in flattened.types if t not in flattened_types]
                    )
                elif flattened not in flattened_types:
                    # Append non-union types directly
                    flattened_types.append(flattened)

            return UnionType(types=flattened_types, **recap_type.extra_attrs)
        elif isinstance(recap_type, StructType):
            return StructType(
                fields=[self._flatten_unions(field) for field in recap_type.fields],
                **recap_type.extra_attrs,
            )
        elif isinstance(recap_type, ListType):
            return ListType(
                values=self._flatten_unions(recap_type.values),
                extra_attrs=recap_type.extra_attrs,
            )
        elif isinstance(recap_type, MapType):
            return MapType(
                keys=self._flatten_unions(recap_type.keys),
                values=self._flatten_unions(recap_type.values),
                extra_attrs=recap_type.extra_attrs,
            )
        else:
            # No nested union type to flatten
            return recap_type