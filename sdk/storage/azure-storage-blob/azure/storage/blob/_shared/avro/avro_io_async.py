# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------

"""Input/output utilities.

Includes:
 - i/o-specific constants
 - i/o-specific exceptions
 - schema validation
 - leaf value encoding and decoding
 - datum reader/writer stuff (?)

Also includes a generic representation for data, which uses the
following mapping:
 - Schema records are implemented as dict.
 - Schema arrays are implemented as list.
 - Schema maps are implemented as dict.
 - Schema strings are implemented as unicode.
 - Schema bytes are implemented as str.
 - Schema ints are implemented as int.
 - Schema longs are implemented as long.
 - Schema floats are implemented as float.
 - Schema doubles are implemented as float.
 - Schema booleans are implemented as bool.
"""

import binascii
import logging
import sys

from ..avro import schema

from .avro_io import STRUCT_FLOAT, STRUCT_DOUBLE, STRUCT_CRC32, SchemaResolutionException

PY3 = sys.version_info[0] == 3

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Decoder/Encoder


class AsyncBinaryDecoder(object):
    """Read leaf values."""

    def __init__(self, reader):
        """
        reader is a Python object on which we can call read, seek, and tell.
        """
        self._reader = reader

    @property
    def reader(self):
        """Reports the reader used by this decoder."""
        return self._reader

    async def read(self, n):
        """Read n bytes.

        Args:
          n: Number of bytes to read.
        Returns:
          The next n bytes from the input.
        """
        assert (n >= 0), n
        input_bytes = await self.reader.read(n)
        if n > 0 and not input_bytes:
            raise StopAsyncIteration
        assert (len(input_bytes) == n), input_bytes
        return input_bytes

    @staticmethod
    def read_null():
        """
        null is written as zero bytes
        """
        return None

    async def read_boolean(self):
        """
        a boolean is written as a single byte
        whose value is either 0 (false) or 1 (true).
        """
        return ord(await self.read(1)) == 1

    async def read_int(self):
        """
        int and long values are written using variable-length, zig-zag coding.
        """
        return await self.read_long()

    async def read_long(self):
        """
        int and long values are written using variable-length, zig-zag coding.
        """
        b = ord(await self.read(1))
        n = b & 0x7F
        shift = 7
        while (b & 0x80) != 0:
            b = ord(await self.read(1))
            n |= (b & 0x7F) << shift
            shift += 7
        datum = (n >> 1) ^ -(n & 1)
        return datum

    async def read_float(self):
        """
        A float is written as 4 bytes.
        The float is converted into a 32-bit integer using a method equivalent to
        Java's floatToIntBits and then encoded in little-endian format.
        """
        return STRUCT_FLOAT.unpack(await self.read(4))[0]

    async def read_double(self):
        """
        A double is written as 8 bytes.
        The double is converted into a 64-bit integer using a method equivalent to
        Java's doubleToLongBits and then encoded in little-endian format.
        """
        return STRUCT_DOUBLE.unpack(await self.read(8))[0]

    async def read_bytes(self):
        """
        Bytes are encoded as a long followed by that many bytes of data.
        """
        nbytes = await self.read_long()
        assert (nbytes >= 0), nbytes
        return await self.read(nbytes)

    async def read_utf8(self):
        """
        A string is encoded as a long followed by
        that many bytes of UTF-8 encoded character data.
        """
        input_bytes = await self.read_bytes()
        if PY3:
            try:
                return input_bytes.decode('utf-8')
            except UnicodeDecodeError as exn:
                logger.error('Invalid UTF-8 input bytes: %r', input_bytes)
                raise exn
        else:
            # PY2
            return unicode(input_bytes, "utf-8") # pylint: disable=undefined-variable

    async def check_crc32(self, input_bytes):
        checksum = STRUCT_CRC32.unpack(await self.read(4))[0]
        if binascii.crc32(input_bytes) & 0xffffffff != checksum:
            raise schema.AvroException("Checksum failure")

    def skip_null(self):
        pass

    async def skip_boolean(self):
        await self.skip(1)

    async def skip_int(self):
        await self.skip_long()

    async def skip_long(self):
        b = ord(await self.read(1))
        while (b & 0x80) != 0:
            b = ord(await self.read(1))

    async def skip_float(self):
        await self.skip(4)

    async def skip_double(self):
        await self.skip(8)

    async def skip_bytes(self):
        await self.skip(await self.read_long())

    async def skip_utf8(self):
        await self.skip_bytes()

    async def skip(self, n):
        await self.reader.seek(await self.reader.tell() + n)


# ------------------------------------------------------------------------------
# DatumReader/Writer


class AsyncDatumReader(object):
    """Deserialize Avro-encoded data into a Python data structure."""

    @staticmethod
    def check_props(schema_one, schema_two, prop_list):
        for prop in prop_list:
            if getattr(schema_one, prop) != getattr(schema_two, prop):
                return False
        return True

    @staticmethod
    def match_schemas(writer_schema, reader_schema):
        w_type = writer_schema.type
        r_type = reader_schema.type
        if 'union' in [w_type, r_type] or 'error_union' in [w_type, r_type]:
            result = True
        elif (w_type in schema.PRIMITIVE_TYPES and r_type in schema.PRIMITIVE_TYPES
              and w_type == r_type):
            result = True
        elif (w_type == r_type == 'record' and
              AsyncDatumReader.check_props(writer_schema, reader_schema,
                                      ['fullname'])):
            result = True
        elif (w_type == r_type == 'error' and
              AsyncDatumReader.check_props(writer_schema, reader_schema,
                                      ['fullname'])):
            result = True
        elif w_type == r_type == 'request':
            return True
        elif (w_type == r_type == 'fixed' and
              AsyncDatumReader.check_props(writer_schema, reader_schema,
                                      ['fullname', 'size'])):
            result = True
        elif (w_type == r_type == 'enum' and
              AsyncDatumReader.check_props(writer_schema, reader_schema,
                                      ['fullname'])):
            result = True
        elif (w_type == r_type == 'map' and
              AsyncDatumReader.check_props(writer_schema.values,
                                      reader_schema.values, ['type'])):
            result = True
        elif (w_type == r_type == 'array' and
              AsyncDatumReader.check_props(writer_schema.items,
                                      reader_schema.items, ['type'])):
            result = True

        # Handle schema promotion
        elif w_type == 'int' and r_type in ['long', 'float', 'double']:
            result = True
        elif w_type == 'long' and r_type in ['float', 'double']:
            result = True
        elif w_type == 'float' and r_type == 'double':
            result = True
        else:
            result = False
        return result

    def __init__(self, writer_schema=None, reader_schema=None):
        """
        As defined in the Avro specification, we call the schema encoded
        in the data the "writer's schema", and the schema expected by the
        reader the "reader's schema".
        """
        self._writer_schema = writer_schema
        self._reader_schema = reader_schema

    # read/write properties
    def set_writer_schema(self, writer_schema):
        self._writer_schema = writer_schema

    writer_schema = property(lambda self: self._writer_schema,
                             set_writer_schema)

    def set_reader_schema(self, reader_schema):
        self._reader_schema = reader_schema

    reader_schema = property(lambda self: self._reader_schema,
                             set_reader_schema)

    async def read(self, decoder):
        if self.reader_schema is None:
            self.reader_schema = self.writer_schema
        return await self.read_data(self.writer_schema, self.reader_schema, decoder)

    async def read_data(self, writer_schema, reader_schema, decoder):
        # schema matching
        if not AsyncDatumReader.match_schemas(writer_schema, reader_schema):
            fail_msg = 'Schemas do not match.'
            raise SchemaResolutionException(fail_msg, writer_schema, reader_schema)

        # schema resolution: reader's schema is a union, writer's schema is not
        if (writer_schema.type not in ['union', 'error_union']
                and reader_schema.type in ['union', 'error_union']):
            for s in reader_schema.schemas:
                if AsyncDatumReader.match_schemas(writer_schema, s):
                    return await self.read_data(writer_schema, s, decoder)
            fail_msg = 'Schemas do not match.'
            raise SchemaResolutionException(fail_msg, writer_schema, reader_schema)

        # function dispatch for reading data based on type of writer's schema
        if writer_schema.type == 'null':
            result = decoder.read_null()
        elif writer_schema.type == 'boolean':
            result = await decoder.read_boolean()
        elif writer_schema.type == 'string':
            result = await decoder.read_utf8()
        elif writer_schema.type == 'int':
            result = await decoder.read_int()
        elif writer_schema.type == 'long':
            result = await decoder.read_long()
        elif writer_schema.type == 'float':
            result = await decoder.read_float()
        elif writer_schema.type == 'double':
            result = await decoder.read_double()
        elif writer_schema.type == 'bytes':
            result = await decoder.read_bytes()
        elif writer_schema.type == 'fixed':
            result = await self.read_fixed(writer_schema, decoder)
        elif writer_schema.type == 'enum':
            result = await self.read_enum(writer_schema, reader_schema, decoder)
        elif writer_schema.type == 'array':
            result = await self.read_array(writer_schema, reader_schema, decoder)
        elif writer_schema.type == 'map':
            result = await self.read_map(writer_schema, reader_schema, decoder)
        elif writer_schema.type in ['union', 'error_union']:
            result = await self.read_union(writer_schema, reader_schema, decoder)
        elif writer_schema.type in ['record', 'error', 'request']:
            result = await self.read_record(writer_schema, reader_schema, decoder)
        else:
            fail_msg = "Cannot read unknown schema type: %s" % writer_schema.type
            raise schema.AvroException(fail_msg)
        return result

    async def skip_data(self, writer_schema, decoder):
        if writer_schema.type == 'null':
            result = decoder.skip_null()
        elif writer_schema.type == 'boolean':
            result = await decoder.skip_boolean()
        elif writer_schema.type == 'string':
            result = await decoder.skip_utf8()
        elif writer_schema.type == 'int':
            result = await decoder.skip_int()
        elif writer_schema.type == 'long':
            result = await decoder.skip_long()
        elif writer_schema.type == 'float':
            result = await decoder.skip_float()
        elif writer_schema.type == 'double':
            result = await decoder.skip_double()
        elif writer_schema.type == 'bytes':
            result = await decoder.skip_bytes()
        elif writer_schema.type == 'fixed':
            result = await self.skip_fixed(writer_schema, decoder)
        elif writer_schema.type == 'enum':
            result = await self.skip_enum(decoder)
        elif writer_schema.type == 'array':
            await self.skip_array(writer_schema, decoder)
            result = None
        elif writer_schema.type == 'map':
            await self.skip_map(writer_schema, decoder)
            result = None
        elif writer_schema.type in ['union', 'error_union']:
            result = await self.skip_union(writer_schema, decoder)
        elif writer_schema.type in ['record', 'error', 'request']:
            await self.skip_record(writer_schema, decoder)
            result = None
        else:
            fail_msg = "Unknown schema type: %s" % writer_schema.type
            raise schema.AvroException(fail_msg)
        return result

    @staticmethod
    async def read_fixed(writer_schema, decoder):
        """
        Fixed instances are encoded using the number of bytes declared
        in the schema.
        """
        return await decoder.read(writer_schema.size)

    @staticmethod
    async def skip_fixed(writer_schema, decoder):
        return await decoder.skip(writer_schema.size)

    @staticmethod
    async def read_enum(writer_schema, reader_schema, decoder):
        """
        An enum is encoded by a int, representing the zero-based position
        of the symbol in the schema.
        """
        # read data
        index_of_symbol = await decoder.read_int()
        if index_of_symbol >= len(writer_schema.symbols):
            fail_msg = "Can't access enum index %d for enum with %d symbols" \
                       % (index_of_symbol, len(writer_schema.symbols))
            raise SchemaResolutionException(fail_msg, writer_schema, reader_schema)
        read_symbol = writer_schema.symbols[index_of_symbol]

        # schema resolution
        if read_symbol not in reader_schema.symbols:
            fail_msg = "Symbol %s not present in Reader's Schema" % read_symbol
            raise SchemaResolutionException(fail_msg, writer_schema, reader_schema)

        return read_symbol

    @staticmethod
    async def skip_enum(decoder):
        return await decoder.skip_int()

    async def read_array(self, writer_schema, reader_schema, decoder):
        """
        Arrays are encoded as a series of blocks.

        Each block consists of a long count value,
        followed by that many array items.
        A block with count zero indicates the end of the array.
        Each item is encoded per the array's item schema.

        If a block's count is negative,
        then the count is followed immediately by a long block size,
        indicating the number of bytes in the block.
        The actual count in this case
        is the absolute value of the count written.
        """
        read_items = []
        block_count = await decoder.read_long()
        while block_count != 0:
            if block_count < 0:
                block_count = -block_count
                await decoder.read_long()
            for _ in range(block_count):
                read_items.append(await self.read_data(writer_schema.items,
                                                 reader_schema.items, decoder))
            block_count = await decoder.read_long()
        return read_items

    async def skip_array(self, writer_schema, decoder):
        block_count = await decoder.read_long()
        while block_count != 0:
            if block_count < 0:
                block_size = await decoder.read_long()
                await decoder.skip(block_size)
            else:
                for _ in range(block_count):
                    await self.skip_data(writer_schema.items, decoder)
            block_count = await decoder.read_long()

    async def read_map(self, writer_schema, reader_schema, decoder):
        """
        Maps are encoded as a series of blocks.

        Each block consists of a long count value,
        followed by that many key/value pairs.
        A block with count zero indicates the end of the map.
        Each item is encoded per the map's value schema.

        If a block's count is negative,
        then the count is followed immediately by a long block size,
        indicating the number of bytes in the block.
        The actual count in this case
        is the absolute value of the count written.
        """
        read_items = {}
        block_count = await decoder.read_long()
        while block_count != 0:
            if block_count < 0:
                block_count = -block_count
                await decoder.read_long()
            for _ in range(block_count):
                key = await decoder.read_utf8()
                read_items[key] = await self.read_data(writer_schema.values,
                                                 reader_schema.values, decoder)
            block_count = await decoder.read_long()
        return read_items

    async def skip_map(self, writer_schema, decoder):
        block_count = await decoder.read_long()
        while block_count != 0:
            if block_count < 0:
                block_size = await decoder.read_long()
                await decoder.skip(block_size)
            else:
                for _ in range(block_count):
                    await decoder.skip_utf8()
                    await self.skip_data(writer_schema.values, decoder)
            block_count = await decoder.read_long()

    async def read_union(self, writer_schema, reader_schema, decoder):
        """
        A union is encoded by first writing a long value indicating
        the zero-based position within the union of the schema of its value.
        The value is then encoded per the indicated schema within the union.
        """
        # schema resolution
        index_of_schema = int(await decoder.read_long())
        if index_of_schema >= len(writer_schema.schemas):
            fail_msg = "Can't access branch index %d for union with %d branches" \
                       % (index_of_schema, len(writer_schema.schemas))
            raise SchemaResolutionException(fail_msg, writer_schema, reader_schema)
        selected_writer_schema = writer_schema.schemas[index_of_schema]

        # read data
        return await self.read_data(selected_writer_schema, reader_schema, decoder)

    async def skip_union(self, writer_schema, decoder):
        index_of_schema = int(await decoder.read_long())
        if index_of_schema >= len(writer_schema.schemas):
            fail_msg = "Can't access branch index %d for union with %d branches" \
                       % (index_of_schema, len(writer_schema.schemas))
            raise SchemaResolutionException(fail_msg, writer_schema)
        return await self.skip_data(writer_schema.schemas[index_of_schema], decoder)

    async def read_record(self, writer_schema, reader_schema, decoder):
        """
        A record is encoded by encoding the values of its fields
        in the order that they are declared. In other words, a record
        is encoded as just the concatenation of the encodings of its fields.
        Field values are encoded per their schema.

        Schema Resolution:
         * the ordering of fields may be different: fields are matched by name.
         * schemas for fields with the same name in both records are resolved
           recursively.
         * if the writer's record contains a field with a name not present in the
           reader's record, the writer's value for that field is ignored.
         * if the reader's record schema has a field that contains a default value,
           and writer's schema does not have a field with the same name, then the
           reader should use the default value from its field.
         * if the reader's record schema has a field with no default value, and
           writer's schema does not have a field with the same name, then the
           field's value is unset.
        """
        # schema resolution
        readers_fields_dict = reader_schema.field_map
        read_record = {}
        for field in writer_schema.fields:
            readers_field = readers_fields_dict.get(field.name)
            if readers_field is not None:
                field_val = await self.read_data(field.type, readers_field.type, decoder)
                read_record[field.name] = field_val
            else:
                await self.skip_data(field.type, decoder)

        # fill in default values
        if len(readers_fields_dict) > len(read_record):
            writers_fields_dict = writer_schema.field_map
            for field_name, field in readers_fields_dict.items():
                if field_name not in writers_fields_dict:
                    if field.has_default:
                        field_val = self._read_default_value(field.type, field.default)
                        read_record[field.name] = field_val
                    else:
                        fail_msg = 'No default value for field %s' % field_name
                        raise SchemaResolutionException(fail_msg, writer_schema,
                                                        reader_schema)
        return read_record

    async def skip_record(self, writer_schema, decoder):
        for field in writer_schema.fields:
            await self.skip_data(field.type, decoder)

    def _read_default_value(self, field_schema, default_value):
        """
        Basically a JSON Decoder?
        """
        if field_schema.type == 'null':
            result = None
        elif field_schema.type == 'boolean':
            result = bool(default_value)
        elif field_schema.type == 'int':
            result = int(default_value)
        elif field_schema.type == 'long':
            result = int(default_value)
        elif field_schema.type in ['float', 'double']:
            result = float(default_value)
        elif field_schema.type in ['enum', 'fixed', 'string', 'bytes']:
            result = default_value
        elif field_schema.type == 'array':
            read_array = []
            for json_val in default_value:
                item_val = self._read_default_value(field_schema.items, json_val)
                read_array.append(item_val)
            result = read_array
        elif field_schema.type == 'map':
            read_map = {}
            for key, json_val in default_value.items():
                map_val = self._read_default_value(field_schema.values, json_val)
                read_map[key] = map_val
            result = read_map
        elif field_schema.type in ['union', 'error_union']:
            result = self._read_default_value(field_schema.schemas[0], default_value)
        elif field_schema.type == 'record':
            read_record = {}
            for field in field_schema.fields:
                json_val = default_value.get(field.name)
                if json_val is None:
                    json_val = field.default
                field_val = self._read_default_value(field.type, json_val)
                read_record[field.name] = field_val
            result = read_record
        else:
            fail_msg = 'Unknown type: %s' % field_schema.type
            raise schema.AvroException(fail_msg)
        return result


# ------------------------------------------------------------------------------

if __name__ == '__main__':
    raise Exception('Not a standalone module')
