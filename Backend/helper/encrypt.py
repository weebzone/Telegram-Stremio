import asyncio
import json
import zlib
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor()

BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def compress_data(data):
    return zlib.compress(data.encode(), level=zlib.Z_BEST_COMPRESSION)


def decompress_data(data):
    return zlib.decompress(data).decode()


def base62_encode(data):
    num = int.from_bytes(data, 'big')
    base62 = []
    while num:
        num, rem = divmod(num, 62)
        base62.append(BASE62_ALPHABET[rem])
    return ''.join(reversed(base62)) or '0'


def base62_decode(data):
    num = 0
    for char in data:
        num = num * 62 + BASE62_ALPHABET.index(char)
    return num.to_bytes((num.bit_length() + 7) // 8, 'big') or b'\0'


async def _run(fn, data):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, fn, data)


async def encode_string(data):
    compressed_data = await _run(compress_data, json.dumps(data))
    return await _run(base62_encode, compressed_data)


async def decode_string(encoded_data):
    compressed_data = await _run(base62_decode, encoded_data)
    json_data = await _run(decompress_data, compressed_data)
    return json.loads(json_data)
