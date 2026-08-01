from Backend.logger import LOGGER

STORED = 0


def _u16(b, o):
    return int.from_bytes(b[o:o + 2], "little")


def _u32(b, o):
    return int.from_bytes(b[o:o + 4], "little")


def _u64(b, o):
    return int.from_bytes(b[o:o + 8], "little")


#----- Resolve the real (uncompressed, compressed) sizes from a Zip64 extra field
def _zip64_sizes(extra, uncomp, comp, need_offset=False, offset=0):
    i = 0
    while i + 4 <= len(extra):
        hid = _u16(extra, i)
        hsz = _u16(extra, i + 2)
        body = extra[i + 4:i + 4 + hsz]
        if hid == 0x0001:
            vals = [_u64(body, j) for j in range(0, (len(body) // 8) * 8, 8)]
            k = 0
            if uncomp == 0xFFFFFFFF and k < len(vals):
                uncomp = vals[k]; k += 1
            if comp == 0xFFFFFFFF and k < len(vals):
                comp = vals[k]; k += 1
            if need_offset and offset == 0xFFFFFFFF and k < len(vals):
                offset = vals[k]; k += 1
            break
        i += 4 + hsz
    return uncomp, comp, offset


#----- Parse a local file header (PK\x03\x04) into entry metadata
def parse_local_header(buf):
    if len(buf) < 30 or buf[0:4] != b"PK\x03\x04":
        return None
    flag = _u16(buf, 6)
    method = _u16(buf, 8)
    comp = _u32(buf, 18)
    uncomp = _u32(buf, 22)
    name_len = _u16(buf, 26)
    extra_len = _u16(buf, 28)
    name = buf[30:30 + name_len].decode("utf-8", "ignore")
    extra = buf[30 + name_len:30 + name_len + extra_len]
    if uncomp == 0xFFFFFFFF or comp == 0xFFFFFFFF:
        uncomp, comp, _ = _zip64_sizes(extra, uncomp, comp)
    return {
        "method": method,
        "name": name,
        "data_offset": 30 + name_len + extra_len,
        "size": uncomp,
        "comp_size": comp,
        "has_descriptor": bool(flag & 0x08),
    }


#----- Parse the first central-directory record from a tail buffer
def _parse_central_directory(tail, tail_base, zip_size):
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        return None
    cd_offset = _u32(tail, eocd + 16)

    z64loc = tail.rfind(b"PK\x06\x07")
    if cd_offset == 0xFFFFFFFF and z64loc >= 0:
        z64_eocd_off = _u64(tail, z64loc + 8)
        rel = z64_eocd_off - tail_base
        if 0 <= rel < len(tail) and tail[rel:rel + 4] == b"PK\x06\x06":
            cd_offset = _u64(tail, rel + 48)

    rel_cd = cd_offset - tail_base
    if rel_cd < 0 or rel_cd + 46 > len(tail) or tail[rel_cd:rel_cd + 4] != b"PK\x01\x02":
        return None

    b = tail
    o = rel_cd
    method = _u16(b, o + 10)
    comp = _u32(b, o + 20)
    uncomp = _u32(b, o + 24)
    name_len = _u16(b, o + 28)
    extra_len = _u16(b, o + 30)
    comment_len = _u16(b, o + 32)
    local_offset = _u32(b, o + 42)
    name = b[o + 46:o + 46 + name_len].decode("utf-8", "ignore")
    extra = b[o + 46 + name_len:o + 46 + name_len + extra_len]
    if uncomp == 0xFFFFFFFF or comp == 0xFFFFFFFF or local_offset == 0xFFFFFFFF:
        uncomp, comp, local_offset = _zip64_sizes(extra, uncomp, comp, need_offset=True, offset=local_offset)
    return {"method": method, "name": name, "size": uncomp, "comp_size": comp, "local_offset": local_offset}


#----- Locate the streamable (STORED) inner file inside a (possibly split) zip.
#----- `read` is an async callable read(start, length) -> bytes over the concatenated zip.
async def resolve_zip_entry(read, zip_size):
    try:
        head = await read(0, min(65536, zip_size))
        lh = parse_local_header(head)
        if (lh and lh["method"] == STORED and lh["size"] > 0
                and not lh["has_descriptor"]
                and lh["data_offset"] + lh["size"] <= zip_size):
            return lh

        tail_len = min(262144, zip_size)
        tail = await read(zip_size - tail_len, tail_len)
        cd = _parse_central_directory(tail, zip_size - tail_len, zip_size)
        if not cd or cd["method"] != STORED or cd["size"] <= 0:
            return lh

        lh_buf = await read(cd["local_offset"], min(4096, zip_size - cd["local_offset"]))
        lh2 = parse_local_header(lh_buf)
        if not lh2:
            return None
        data_offset = cd["local_offset"] + lh2["data_offset"]
        if data_offset + cd["size"] > zip_size:
            return None
        return {"method": STORED, "name": cd["name"], "data_offset": data_offset,
                "size": cd["size"], "comp_size": cd["comp_size"], "has_descriptor": False}
    except Exception as e:
        LOGGER.warning(f"[ZIP] Failed to resolve inner entry: {e}")
        return None
