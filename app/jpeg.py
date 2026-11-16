import struct


def jpeg_size(data):
    if data[:2] != b"\xff\xd8":
        raise ValueError("JPEG形式で送信してください。")
    p = 2
    while p < len(data) and data[p] == 255:
        while p < len(data) and data[p] == 255:
            p += 1
        if p >= len(data):
            break
        mark = data[p]
        p += 1
        if mark in (0xDA, 0xD9):
            break
        if mark == 1 or 0xD0 <= mark <= 0xD8:
            continue
        if p + 2 > len(data):
            break
        size = struct.unpack_from(">H", data, p)[0]
        if size < 2 or p + size > len(data):
            break
        if mark in (0xC0, 0xC1, 0xC2) and size >= 8:
            h, w = struct.unpack_from(">HH", data, p + 3)
            if not (0 < w <= 3840 and 0 < h <= 2160):
                raise ValueError("画像は3840×2160以下にしてください。")
            return w, h
        p += size
    raise ValueError("JPEG画像を確認できません。")
