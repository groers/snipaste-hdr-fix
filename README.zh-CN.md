# snipaste-hdr-fix

修复 **Snipaste** 在 HDR 显示器上截图**发灰 / 发白**的问题。

> English → [README.md](README.md)

---

## 问题现象

Windows 开启 HDR 后，Snipaste 2.11.x 的两种设置**都不对**：

| 设置 | 结果 |
|---|---|
| "调整 HDR 显示器上截图的颜色" **勾选** | 高光被逐级压暗，整幅**发灰**、不通透 |
| 同一选项 **取消勾选** | 整体被抬升，亮部溢出、暗部发白 |

在 4K QD-MiniLED 屏（175% 缩放）上，用截图与"屏幕上实际显示的内容"逐分位对比：

| 亮度分位 | 屏幕真值 | 勾选（原版 bug） | 取消勾选（原版 bug） | 本工具修复后 |
|---|---|---|---|---|
| p90 | 228 | **213（−15）** | 254（+26） | 228 ✓ |
| p99 | 245 | **220（−25）** | 254（+9） | 245 ✓ |
| 最亮 | 254 | **224（−30）** | 254 | 254 ✓ |

视觉对比见 `docs/comparison.png`。

**为什么两张照片看起来几乎一样**：差异只影响亮度的最顶端——该样张里 **75% 的像素逐位相同**
（上表 p5 ~ 中位 = ±0）。它在**平坦的白底 / 浅色界面**上一眼可见（白 254 → 224，即
#FEFEFE 与 #E0E0E0 之差，观感就是"整体发灰、文字对比度下降"），而在照片的平滑高光渐变里
几乎看不出——因为人眼对绝对亮度没有参照。

两个更直观的看法：

- **[`docs/comparison-split.png`](docs/comparison-split.png)**：同一张照片沿一条接缝一分为二，
  左侧 = 原图，右侧 = **同一批像素**经着色器公式换算后的结果。天空处可见台阶，图内附 3.9× 放大窗
- **[`docs/white-level.png`](docs/white-level.png)**：254 与 224 的平色块对照

## 根本原因

Snipaste v2.11 起内置了 [GEEKiDoS/bitblt-hdr](https://github.com/GEEKiDoS/bitblt-hdr) 的
HDR 色调映射着色器。在 `tonemapper.hlsl` 里，SDR 内容会被归一化成 **SDR 白点 = 1.0**：

```hlsl
float3 input_color = clamp(src_color, 0, 10000) / (white_level / 80);  // SDR 白 → 1.0
```

但 `linear_tonemap()` 的拐点却定在 **0.8**：

```hlsl
float3 linear_tonemap(float3 x)
{
    const float z = 0.8;
    const float d = 2.5;
    return lerp(x, (x - z) / d + z, step(z, x));
}
```

于是纯白（1.0）被映射成 `0.8 + (1.0 − 0.8) / 2.5 = **0.88**`，8 位图就是 **224 而不是 255**，
亮度 214 以上全部被压暗。第 112 行的 `step(0.8, linear_luma)` 同理，会让亮度 0.8 以上的
SDR 颜色被中性色调映射降饱和。

## 修复方式

改两个常量，把拐点抬到 SDR 白点：

```diff
-    const float z = 0.8;
+    const float z = 1.0;
...
-        linear_color = lerp(linear_result, neutral_color * linear_luma, step(0.8, linear_luma));
+        linear_color = lerp(linear_result, neutral_color * linear_luma, step(1.0, linear_luma));
```

SDR 内容（≤ 1.0）因此**原样通过**，只有超过 SDR 白点的部分才交给原有的中性色调映射压缩。

**为什么不能直接改字节？** DXBC 容器头带 16 字节的编译器私有哈希，改动任何一个字节都会被
D3D11 拒绝加载（`hr=0x80070057`），Snipaste 随即判定功能不可用、**选项变灰**。
所以本工具是**重新编译着色器**，让编译器自己生成正确的哈希。

## 运行要求与版本兼容性

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 / 11，**x64**（HDR 本身还需 Windows 10 1809+ 且显示器支持 HDR） |
| Snipaste | **2.11.x 桌面版**——**仅实测过 2.11.3** |
| Python | 3.8+（只用标准库，无需 pip 安装任何包） |
| 编译器 | **不需要**——着色器用 Snipaste 目录里自带的 `d3dcompiler_47.dll` 编译 |

不支持 / 未验证的情况：

| 情况 | 结果 |
|---|---|
| **微软商店版** | **不支持**。它的文件在 `Program Files\WindowsApps` 受保护目录里，且商店会自动更新——补丁要么写不进去、要么被静默覆盖。请用桌面版。 |
| Snipaste ≤ 2.10.x | 那一版还没有内置 HDR 校正；工具会报"找不到 DXBC 着色器资源"并中止。 |
| 32 位 Snipaste | **未测试**——PE 解析器能处理 PE32，但所有验证都只在 x64 上做过。 |
| 显示器接在不同显卡上 | 不支持——上游库本身就不处理多 GPU 场景。 |
| 未来重写了着色器的新版本 | **先跑 `check`**。若状态显示 `unknown`（指纹不匹配），工具会**拒绝替换**；否则会把旧着色器覆盖到新逻辑上。 |

> 通用原则：凡不在上表"已实测"范围内的版本，**先 `check` 再决定要不要 `patch`**；
> 若 `check` 报 `unknown`，停手并反馈，不要强行替换。

## 使用方法

把 `snipaste_hdr_fix.py` 和 `tonemapper.hlsl` 放到任意位置（也可以直接放进 Snipaste 目录），然后：

```bash
python snipaste_hdr_fix.py check                 # 查看当前状态，不改任何文件
python snipaste_hdr_fix.py patch                 # 备份 → 编译 → 替换 → 校验
python snipaste_hdr_fix.py restore               # 从备份回滚

# 若脚本不在 Snipaste 目录内：
python snipaste_hdr_fix.py check --dir "D:\Snipaste"
```

**改完必须完全退出 Snipaste 再启动**——着色器是启动时加载的。

几点说明：

- 首次 `patch` 会自动生成备份 `Snipaste.exe.unpatched.bak`
- 写盘前后都会用 D3D11 实际创建一次着色器做校验；校验不过就拒绝写入
- Snipaste 正在运行时，工具会先把 exe 改名让路，而不是直接失败
- `check` 会区分三种状态：原版（未修复）/ 已修复 / 异常（被直接改过字节）

## 注意事项（请务必读完）

- **非官方**：与 Snipaste、bitblt-hdr 项目均无隶属关系。它修改的是你本机的第三方程序，
  风险自负；工具会自动备份并支持一键回滚。
- **这是刻意的取舍**：对于超过 SDR 白点的内容（HDR 游戏/视频），压缩起点从原来的 1.3 倍
  提前到了 1.0 倍。在 8 位 SDR 输出里，"保住这段高光层次"和"SDR 白必须是 255"**互相排斥**，
  本工具选择保住 SDR 的准确性。日常截图（网页 / 界面 / 照片）没有任何损失。
- **Snipaste 升级会覆盖 exe**，每次升级后需要重新执行一次 `patch`。
- 显示器分别接在不同显卡上的多 GPU 环境不适用（上游库本身就不支持）。

## 署名

- 着色器衍生自 **[GEEKiDoS/bitblt-hdr](https://github.com/GEEKiDoS/bitblt-hdr)**（MIT），详见 [NOTICE](NOTICE)
- Snipaste — <https://www.snipaste.com/>

## 法律说明

- 本仓库发布的是**工具，不是改好的程序**——它修改的是**你机器上已有的那份** Snipaste 副本；
  你需要自己拥有合法副本才能使用。
- **本仓库不含任何 Snipaste 二进制、资源或源码。** Snipaste 的 EULA 也禁止再分发其软件，
  请不要把 Snipaste 的文件加进本仓库。
- Snipaste 的最终用户许可协议**禁止对其软件进行修改 / 反向工程 / 反编译**；运行本工具即意味着
  修改你自己的那份副本，这属于该协议限制的行为——请自行判断是否可接受。详见 [NOTICE](NOTICE)。
- 与 Snipaste 官方无隶属关系；**不涉及任何授权、DRM 或付费功能的绕过**。

## 许可

MIT，见 [LICENSE](LICENSE)。上游着色器署名见 [NOTICE](NOTICE) 与
[LICENSE-bitblt-hdr](LICENSE-bitblt-hdr)。