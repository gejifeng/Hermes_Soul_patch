"""Zero-dependency QR Code SVG generator (ISO/IEC 18004, byte mode, versions 1-10).

Ported from ssamurias/dsh-fish `lib/qr.js` so login URLs (`https://txz.qq.com/p?k=...`)
are rendered into SVG/Data-URI locally on the host without leaking keys to external services.
"""
import base64

EXP = [0] * 512
LOG = [0] * 256


def _init_gf():
    x = 1
    for i in range(255):
        EXP[i] = x
        LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        EXP[i] = EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def _rs_generator(deg: int) -> list[int]:
    poly = [1]
    for i in range(deg):
        nxt = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            nxt[j] ^= coeff
            nxt[j + 1] ^= _gf_mul(coeff, EXP[i])
        poly = nxt
    return poly


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_generator(ec_len)
    rem = list(data) + [0] * ec_len
    for i in range(len(data)):
        factor = rem[i]
        if factor == 0:
            continue
        for j, g in enumerate(gen):
            rem[i + j] ^= _gf_mul(g, factor)
    return rem[len(data):]


ECC_BLOCKS = {
    1: {"L": (7, 1, 19, 0, 0), "M": (10, 1, 16, 0, 0), "Q": (13, 1, 13, 0, 0), "H": (17, 1, 9, 0, 0)},
    2: {"L": (10, 1, 34, 0, 0), "M": (16, 1, 28, 0, 0), "Q": (22, 1, 22, 0, 0), "H": (28, 1, 16, 0, 0)},
    3: {"L": (15, 1, 55, 0, 0), "M": (26, 1, 44, 0, 0), "Q": (18, 2, 17, 0, 0), "H": (22, 2, 13, 0, 0)},
    4: {"L": (20, 1, 80, 0, 0), "M": (18, 2, 32, 0, 0), "Q": (26, 2, 24, 0, 0), "H": (16, 4, 9, 0, 0)},
    5: {"L": (26, 1, 108, 0, 0), "M": (24, 2, 43, 0, 0), "Q": (18, 2, 15, 2, 16), "H": (22, 2, 11, 2, 12)},
    6: {"L": (18, 2, 68, 0, 0), "M": (16, 4, 27, 0, 0), "Q": (24, 4, 19, 0, 0), "H": (28, 4, 15, 0, 0)},
    7: {"L": (20, 2, 78, 0, 0), "M": (18, 4, 31, 0, 0), "Q": (18, 2, 14, 4, 15), "H": (26, 4, 13, 1, 14)},
    8: {"L": (24, 2, 97, 0, 0), "M": (22, 2, 38, 2, 39), "Q": (22, 4, 18, 2, 19), "H": (26, 4, 14, 2, 15)},
    9: {"L": (30, 2, 116, 0, 0), "M": (22, 3, 36, 2, 37), "Q": (20, 4, 16, 4, 17), "H": (24, 4, 12, 4, 13)},
    10: {"L": (18, 2, 68, 2, 69), "M": (26, 4, 43, 1, 44), "Q": (24, 6, 19, 2, 20), "H": (28, 6, 15, 2, 16)},
}

ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

EC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}
MAX_VERSION = 10


def _push_bits(bits: list[int], value: int, length: int):
    for i in range(length - 1, -1, -1):
        bits.append((value >> i) & 1)


def _choose_version(byte_len: int, ec: str) -> int:
    for v in range(1, MAX_VERSION + 1):
        _, b1, s1, b2, s2 = ECC_BLOCKS[v][ec]
        cap = (b1 * s1 + b2 * s2) * 8
        header = 4 + (8 if v <= 9 else 16)
        if header + byte_len * 8 <= cap:
            return v
    raise ValueError(f"二维码内容过长（{byte_len} 字节）")


def _build_codewords(raw: bytes, version: int, ec: str) -> list[int]:
    ec_len, b1, s1, b2, s2 = ECC_BLOCKS[version][ec]
    total = b1 * s1 + b2 * s2
    bits: list[int] = []
    _push_bits(bits, 0b0100, 4)
    _push_bits(bits, len(raw), 8 if version <= 9 else 16)
    for b in raw:
        _push_bits(bits, b, 8)
    cap_bits = total * 8
    term = min(4, cap_bits - len(bits))
    if term > 0:
        _push_bits(bits, 0, term)
    while len(bits) % 8 != 0:
        bits.append(0)
    k = 0
    while len(bits) < cap_bits:
        _push_bits(bits, 0xEC if k % 2 == 0 else 0x11, 8)
        k += 1
    bytes_out = []
    for i in range(total):
        v = 0
        for j in range(8):
            v = (v << 1) | bits[i * 8 + j]
        bytes_out.append(v)

    data_blocks = []
    ec_blocks = []
    p = 0
    for _ in range(b1):
        chunk = bytes_out[p:p + s1]
        p += s1
        data_blocks.append(chunk)
        ec_blocks.append(_rs_encode(chunk, ec_len))
    for _ in range(b2):
        chunk = bytes_out[p:p + s2]
        p += s2
        data_blocks.append(chunk)
        ec_blocks.append(_rs_encode(chunk, ec_len))

    out = []
    max_data = max(s1, s2)
    for i in range(max_data):
        for blk in data_blocks:
            if i < len(blk):
                out.append(blk[i])
    for i in range(ec_len):
        for blk in ec_blocks:
            if i < len(blk):
                out.append(blk[i])
    return out


def _place_finder(m, r: int, c: int):
    n = len(m)
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            y, x = r + dr, c + dc
            if 0 <= y < n and 0 <= x < n:
                in_ring = 0 <= dr <= 6 and 0 <= dc <= 6
                dark = in_ring and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                m[y][x] = 1 if dark else 0


def _place_alignment(m, version: int):
    n = len(m)
    pos = ALIGN[version]
    for r in pos:
        for c in pos:
            if (r == 6 and c == 6) or (r == 6 and c == n - 7) or (r == n - 7 and c == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    m[r + dr][c + dc] = 1 if dark else 0


def _place_timing(m):
    n = len(m)
    for i in range(8, n - 8):
        bit = 1 if i % 2 == 0 else 0
        if m[6][i] is None:
            m[6][i] = bit
        if m[i][6] is None:
            m[i][6] = bit


def _format_bits(ec: str, mask: int) -> int:
    data = (EC_BITS[ec] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def _reserve_format(m):
    n = len(m)
    for i in range(9):
        if m[8][i] is None:
            m[8][i] = 2
        if m[i][8] is None:
            m[i][8] = 2
    for i in range(8):
        if m[8][n - 1 - i] is None:
            m[8][n - 1 - i] = 2
        if m[n - 1 - i][8] is None:
            m[n - 1 - i][8] = 2
    m[n - 8][8] = 1


def _reserve_version(m, version: int):
    if version < 7:
        return
    n = len(m)
    bits = _version_bits(version)
    for i in range(18):
        bit = (bits >> i) & 1
        a, b = i // 3, i % 3
        m[n - 11 + b][a] = bit
        m[a][n - 11 + b] = bit


def _place_data(m, codewords: list[int]):
    n = len(m)
    is_data = [[False] * n for _ in range(n)]
    bit_idx = 0
    total_bits = len(codewords) * 8
    upward = True
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            y = (n - 1 - vert) if upward else vert
            for j in range(2):
                x = right - j
                if m[y][x] is not None:
                    continue
                bit = 0
                if bit_idx < total_bits:
                    bit = (codewords[bit_idx >> 3] >> (7 - (bit_idx & 7))) & 1
                m[y][x] = bit
                is_data[y][x] = True
                bit_idx += 1
        upward = not upward
        right -= 2
    return is_data


MASKS = (
    lambda y, x: (y + x) % 2 == 0,
    lambda y, x: y % 2 == 0,
    lambda y, x: x % 3 == 0,
    lambda y, x: (y + x) % 3 == 0,
    lambda y, x: ((y // 2) + (x // 3)) % 2 == 0,
    lambda y, x: ((y * x) % 2) + ((y * x) % 3) == 0,
    lambda y, x: (((y * x) % 2) + ((y * x) % 3)) % 2 == 0,
    lambda y, x: (((y + x) % 2) + ((y * x) % 3)) % 2 == 0,
)


def _apply_mask(m, mask: int, is_data):
    n = len(m)
    fn = MASKS[mask]
    for y in range(n):
        for x in range(n):
            if is_data[y][x] and fn(y, x):
                m[y][x] ^= 1


def _write_format(m, ec: str, mask: int):
    n = len(m)
    bits = _format_bits(ec, mask)
    for i in range(6):
        m[i][8] = (bits >> i) & 1
    m[7][8] = (bits >> 6) & 1
    m[8][8] = (bits >> 7) & 1
    m[8][7] = (bits >> 8) & 1
    for i in range(9, 15):
        m[8][14 - i] = (bits >> i) & 1
    for i in range(8):
        m[8][n - 1 - i] = (bits >> i) & 1
    for i in range(8, 15):
        m[n - 15 + i][8] = (bits >> i) & 1
    m[n - 8][8] = 1


def _penalty(m) -> int:
    n = len(m)
    score = 0
    for y in range(n):
        run = 1
        for x in range(1, n):
            if m[y][x] == m[y][x - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for x in range(n):
        run = 1
        for y in range(1, n):
            if m[y][x] == m[y - 1][x]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for y in range(n - 1):
        for x in range(n - 1):
            v = m[y][x]
            if v == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(row) for row in m)
    ratio = (dark * 100) / (n * n)
    score += int(abs(ratio - 50) // 5) * 10
    return score


def encode_qr(text: str, ec: str = "M"):
    ec = ec.upper()
    raw = str(text or "").encode("utf-8")
    if not raw:
        raise ValueError("二维码内容为空")
    version = _choose_version(len(raw), ec)
    codewords = _build_codewords(raw, version, ec)
    size = 17 + version * 4
    best_score = None
    best_modules = None
    for mask in range(8):
        m = [[None] * size for _ in range(size)]
        _place_finder(m, 0, 0)
        _place_finder(m, 0, size - 7)
        _place_finder(m, size - 7, 0)
        _place_alignment(m, version)
        _place_timing(m)
        _reserve_format(m)
        _reserve_version(m, version)
        is_data = _place_data(m, codewords)
        _apply_mask(m, mask, is_data)
        _write_format(m, ec, mask)
        sc = _penalty(m)
        if best_score is None or sc < best_score:
            best_score = sc
            best_modules = m
    return {"version": version, "size": size, "modules": best_modules}


def qr_svg(text: str, *, ec: str = "M", scale: int = 6, margin: int = 3) -> str:
    res = encode_qr(text, ec=ec)
    size = res["size"]
    modules = res["modules"]
    dim = (size + margin * 2) * scale
    paths = []
    for y in range(size):
        for x in range(size):
            if modules[y][x] == 1:
                px = (x + margin) * scale
                py = (y + margin) * scale
                paths.append(f"M{px} {py}h{scale}v{scale}h-{scale}z")
    d_attr = "".join(paths)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" '
        f'viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" fill="#fff"/>'
        f'<path d="{d_attr}" fill="#000"/></svg>'
    )


def qr_data_url(text: str, **kw) -> tuple[str, str, str]:
    """Return `(svg_str, base64_svg, data_url)`."""
    svg = qr_svg(text, **kw)
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return svg, b64, f"data:image/svg+xml;base64,{b64}"
