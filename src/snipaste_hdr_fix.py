#!/usr/bin/env python3
"""Snipaste HDR 截图偏色修复工具 (Windows x64)

问题：Snipaste 2.11 起内置的 HDR 颜色校正来自 GEEKiDoS/bitblt-hdr，其着色器
      tonemapper.hlsl 中 linear_tonemap 的拐点定在 0.8，而 SDR 白点归一化后正好是 1.0
      —— 纯白被压成 0.8 + (1.0-0.8)/2.5 = 0.88，8 位图即 224 而非 255，整幅发灰。

做法：用 Snipaste 自带的 d3dcompiler_47.dll 重新编译"拐点抬到 1.0"的着色器，
      得到哈希正确的 DXBC 容器，原大小替换 exe 内嵌的 RCDATA 资源。
      直接改字节是不行的：容器头的 16 字节是编译器私有哈希，改动会令 D3D11 拒绝加载
      （hr=0x80070057），Snipaste 随即判定该功能不可用、选项变灰。

用法（把本文件与 tonemapper.hlsl 放在一起，可放在 Snipaste 目录内）：
    python snipaste_hdr_fix.py check                 # 只看状态，不改文件
    python snipaste_hdr_fix.py patch                 # 备份 + 编译 + 替换 + 校验
    python snipaste_hdr_fix.py restore               # 从备份还原
    python snipaste_hdr_fix.py check --dir "D:\\Snipaste"   # 指定 Snipaste 目录

注意：patch/restore 后需重启 Snipaste 才生效；Snipaste 升级会覆盖 exe，需重新执行 patch。
"""
import argparse
import ctypes
import glob
import os
import shutil
import struct
import sys

HRESULT = ctypes.c_long
OLD_CONST = 0x3F4CCCCD  # 0.8f —— 旧拐点
NEW_CONST = 0x3F800000  # 1.0f —— SDR 白点
EXPECTED_OLD_COUNT = 7  # 旧着色器里 0.8f 的出现次数（2 组 float3 + 1 个标量）


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------
def say(msg=""):
    print(msg, flush=True)


def load_lib(path, name):
    for cand in (path, os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")):
        p = os.path.join(cand, name) if os.path.isdir(cand) else cand
        try:
            return ctypes.WinDLL(p)
        except OSError:
            continue
    raise SystemExit(f"找不到 {name}（Snipaste 目录与 System32 都没有）")


def blob_data(ptr):
    vt = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    getbuf = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vt[3])
    getsize = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vt[4])
    return ctypes.string_at(getbuf(ptr), getsize(ptr))


def create_compute_shader(blob):
    """把 DXBC 容器交给 D3D11 创建计算着色器，返回 (是否接受, hr)。"""
    try:
        d3d11 = ctypes.WinDLL("d3d11.dll")
    except OSError:
        return None, 0
    d3d11.D3D11CreateDevice.restype = HRESULT
    d3d11.D3D11CreateDevice.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p)]
    dev, feat, ctx = ctypes.c_void_p(), ctypes.c_int(), ctypes.c_void_p()
    lv = (ctypes.c_int * 1)(0xB000)  # FL 11_0
    hr = d3d11.D3D11CreateDevice(None, 1, None, 0, lv, 1, 7,
                                 ctypes.byref(dev), ctypes.byref(feat), ctypes.byref(ctx))
    if hr < 0:
        return None, hr
    vt = ctypes.cast(dev, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    create = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(vt[18])
    buf = ctypes.create_string_buffer(blob, len(blob))
    sh = ctypes.c_void_p()
    hr = create(dev, ctypes.addressof(buf), len(blob), None, ctypes.byref(sh))
    return hr >= 0, hr & 0xFFFFFFFF


# ----------------------------------------------------------------------------
# PE 资源定位
# ----------------------------------------------------------------------------
def parse_pe(d):
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    rsrc_rva = struct.unpack_from("<I", d, dd + 2 * 8)[0]
    secoff = opt + optsz
    secs = []
    for i in range(nsec):
        o = secoff + 40 * i
        vsz, va, rsz, praw = struct.unpack_from("<IIII", d, o + 8)
        secs.append((va, max(vsz, rsz), praw))

    def rva2off(rva):
        for va, size, praw in secs:
            if va <= rva < va + size:
                return praw + (rva - va)
        return None
    return rva2off, rva2off(rsrc_rva)


def find_shader_resource(d):
    """返回 [(资源路径, 文件偏移, 声明长度, 数据目录项偏移)]，匹配内容以 DXBC 开头的项。"""
    rva2off, root = parse_pe(d)
    found = []

    def walk(dir_off, path):
        nn, ni = struct.unpack_from("<HH", d, dir_off + 12)
        for i in range(nn + ni):
            e = dir_off + 16 + 8 * i
            name, off = struct.unpack_from("<II", d, e)
            if off & 0x80000000:
                walk(root + (off & 0x7FFFFFFF), path + [name])
            else:
                de = root + off
                rva, size, _cp, _res = struct.unpack_from("<IIII", d, de)
                fo = rva2off(rva)
                if fo and d[fo:fo + 4] == b"DXBC":
                    found.append((path + [name], fo, size, de))
    walk(root, [])
    return found


def count_old_const(blob):
    return blob.count(struct.pack("<I", OLD_CONST))


def analyze(exe_path):
    with open(exe_path, "rb") as f:
        data = bytearray(f.read())
    hits = find_shader_resource(data)
    if not hits:
        raise SystemExit("在 exe 里找不到内嵌的 DXBC 着色器资源（版本可能不受支持）")
    path, off, size, de = hits[0]
    blob = bytes(data[off:off + size])
    accepted, hr = create_compute_shader(blob)
    n_old = count_old_const(blob)
    if accepted is True and n_old == EXPECTED_OLD_COUNT:
        state = "original"
    elif accepted is True and n_old == 0:
        state = "fixed"
    elif accepted is False:
        state = "broken"
    else:
        state = "unknown"
    return data, (path, off, size, de), state, accepted, hr, n_old


# ----------------------------------------------------------------------------
# 编译修正版着色器
# ----------------------------------------------------------------------------
def find_hlsl(script_dir, override=None):
    if override:
        if not os.path.isfile(override):
            raise SystemExit(f"指定的着色器不存在：{override}")
        return override
    cands = [os.path.join(script_dir, "tonemapper.hlsl"),
             os.path.join(os.path.dirname(script_dir), "tonemapper.hlsl"),
             os.path.join(os.path.dirname(os.path.dirname(script_dir)), "tonemapper.hlsl")]
    for c in cands:
        if os.path.isfile(c):
            return c
    raise SystemExit("找不到 tonemapper.hlsl —— 请把它与本脚本放在同一目录")


def compile_fixed_shader(snipaste_dir, hlsl_path):
    comp = load_lib(snipaste_dir, "d3dcompiler_47.dll")
    comp.D3DCompile.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(ctypes.c_void_p)]
    comp.D3DCompile.restype = HRESULT
    src = open(hlsl_path, "rb").read()
    buf = ctypes.create_string_buffer(src, len(src))
    code, err = ctypes.c_void_p(), ctypes.c_void_p()
    hr = comp.D3DCompile(ctypes.addressof(buf), len(src), os.path.basename(hlsl_path).encode(),
                         None, None, b"main", b"cs_5_0", 1 << 15, 0,
                         ctypes.byref(code), ctypes.byref(err))
    if hr < 0:
        msg = blob_data(err).decode("utf-8", "replace") if err else ""
        raise SystemExit(f"着色器编译失败 hr=0x{hr & 0xFFFFFFFF:08X}\n{msg[:800]}")
    blob = blob_data(code)
    ok, hrc = create_compute_shader(blob)
    if ok is not True:
        raise SystemExit(f"编译产物未通过 D3D11 校验 hr=0x{hrc:08X}")
    return blob


# ----------------------------------------------------------------------------
# 写盘（运行中的 exe 不能直接覆盖，先改名让路）
# ----------------------------------------------------------------------------
def replace_locked(exe_path, data, tmp_suffix=".new"):
    tmp = exe_path + tmp_suffix
    with open(tmp, "wb") as f:
        f.write(data)
    moved = exe_path + ".old"
    try:
        if os.path.exists(moved):
            os.remove(moved)
        os.replace(exe_path, moved)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SystemExit(f"无法为 exe 让路（{e}）：请先完全退出 Snipaste 再试")
    os.replace(tmp, exe_path)
    return moved


# ----------------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------------
def cmd_check(args):
    exe = os.path.join(args.dir, "Snipaste.exe")
    if not os.path.isfile(exe):
        raise SystemExit(f"没找到 {exe}（用 --dir 指定 Snipaste 目录）")
    data, (path, off, size, de), state, accepted, hr, n_old = analyze(exe)
    say(f"Snipaste 目录 : {args.dir}")
    say(f"exe 大小      : {len(data)} 字节")
    say(f"着色器资源    : 资源路径 {path}  偏移 {hex(off)}  声明长度 {size}")
    say(f"D3D11 校验    : {'通过' if accepted else '失败'} (hr=0x{hr:08X})" if accepted is not None
        else "D3D11 校验    : 跳过（无法创建 D3D 设备）")
    say(f"旧拐点常量    : {n_old} 处 (期望旧版 {EXPECTED_OLD_COUNT} 处)")
    label = {"original": "未修复（原版着色器，拐点 0.8 → 白场会被压到 224）",
             "fixed": "已修复（拐点 1.0，白场正常）",
             "broken": "异常（着色器被直接改过字节，D3D11 拒绝加载 → 功能会变灰，请执行 patch 覆盖）",
             "unknown": "未知（请把输出反馈给作者）"}[state]
    say(f"当前状态      : {state} —— {label}")
    return 0


def cmd_patch(args):
    exe = os.path.join(args.dir, "Snipaste.exe")
    if not os.path.isfile(exe):
        raise SystemExit(f"没找到 {exe}（用 --dir 指定 Snipaste 目录）")
    data, (path, off, size, de), state, accepted, hr, n_old = analyze(exe)
    if state == "fixed":
        say("已经是修复后的状态，无需重复操作。")
        return 0
    hlsl = find_hlsl(os.path.dirname(os.path.abspath(__file__)), args.hlsl)
    say(f"使用着色器    : {hlsl}")
    blob = compile_fixed_shader(args.dir, hlsl)
    say(f"编译完成      : {len(blob)} 字节，D3D11 校验通过")
    if len(blob) > size:
        raise SystemExit(f"新容器 {len(blob)} 字节大于资源 {size} 字节，本工具不做腾挪，已中止")

    bak = exe + ".unpatched.bak"
    if not os.path.exists(bak):
        shutil.copy2(exe, bak)
        say(f"已备份原文件  : {os.path.basename(bak)}")

    data[off:off + len(blob)] = blob
    if len(blob) < size:
        struct.pack_into("<I", data, de + 4, len(blob))
        say(f"资源声明长度  : 更新为 {len(blob)}")

    ok, hrc = create_compute_shader(bytes(data[off:off + len(blob)]))
    if ok is not True:
        raise SystemExit(f"写盘前校验失败 hr=0x{hrc:08X}，已中止（未改动文件）")

    moved = replace_locked(exe, data)
    try:
        os.remove(moved)
    except OSError:
        pass
    say("替换完成      : 请完全退出并重启 Snipaste 使其生效")
    say("回滚方式      : python snipaste_hdr_fix.py restore")
    return 0


def cmd_restore(args):
    exe = os.path.join(args.dir, "Snipaste.exe")
    bak = exe + ".unpatched.bak"
    if not os.path.isfile(bak):
        raise SystemExit(f"找不到备份 {bak}，无法还原")
    with open(bak, "rb") as f:
        data = f.read()
    replace_locked(exe, data)
    say("已还原为备份版本，请重启 Snipaste")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Snipaste HDR 截图偏色修复工具")
    ap.add_argument("action", choices=["check", "patch", "restore"])
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="Snipaste 所在目录（默认：本脚本所在目录）")
    ap.add_argument("--hlsl", default=None, help="自定义 tonemapper.hlsl 路径")
    args = ap.parse_args()
    if not os.path.isdir(args.dir):
        raise SystemExit(f"目录不存在：{args.dir}")
    return {"check": cmd_check, "patch": cmd_patch, "restore": cmd_restore}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())