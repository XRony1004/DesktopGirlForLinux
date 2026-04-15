#!/usr/bin/env python3
"""
阶段1：AI 逐帧抠图（Robust Video Matting），输出透明 PNG 序列

改进点：
  - 使用 RVM（循环网络），帧间时序连贯，高速运动不丢肢体
  - 软 alpha matte 保留半透明边缘细节
  - 后处理流水线：阴影抑制 + 形态学去噪 + 最大连通区域保留

用法：
  .venv/bin/python remove_bg.py --input video.mp4 --frames-dir dancer/name
  .venv/bin/python remove_bg.py --input video.mp4 --frames-dir dancer/name --display-height 600 --overwrite
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

# ── ANSI 颜色 ──────────────────────────────────────────────────────────────────

BOLD = "\033[1m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
RESET = "\033[0m"


def find_command(*names):
    """Find a command by name, including snap app aliases."""
    for name in names:
        path = shutil.which(name)
        if path:
            return name
    joined = " / ".join(names)
    print(f"{RED}错误：找不到命令：{joined}{RESET}")
    print("请先安装 ffmpeg，并确保 ffmpeg/ffprobe 在 PATH 中。")
    sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser(
        description="视频背景去除（Robust Video Matting）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
{CYAN}示例：{RESET}
  python remove_bg.py --input dance.mp4 --frames-dir dancer/girl
  python remove_bg.py --input 4k.mp4 --downsample-ratio 0.125 --backend cuda
""",
    )
    p.add_argument("--input", default="jean.mp4", help="输入视频文件")
    p.add_argument("--frames-dir", default="dancer/jean",
                   help="输出帧目录（建议格式：dancer/<角色名>）")
    p.add_argument("--display-height", default=450, type=int,
                   help="输出帧高度（像素），宽度按比例缩放")
    p.add_argument("--overwrite", action="store_true",
                   help="强制重新处理（即使帧已存在）")

    # RVM 参数
    p.add_argument("--variant", default="mobilenetv3",
                   choices=["mobilenetv3", "resnet50"],
                   help="RVM 模型变体（mobilenetv3 更快，resnet50 更精细）")
    p.add_argument("--downsample-ratio", default=0.25, type=float,
                   help="推理下采样比例（HD=0.25, 4K=0.125）")
    p.add_argument("--backend", default="auto",
                   choices=["auto", "cpu", "cuda"],
                   help="推理设备（auto 自动检测 GPU）")

    # 后处理参数
    p.add_argument("--alpha-threshold", default=0.3, type=float,
                   help="阴影抑制阈值：alpha 低于此值的区域清零（0~1）")
    p.add_argument("--no-postprocess", action="store_true",
                   help="跳过 mask 后处理（阈值 + 形态学 + 连通区域）")
    return p.parse_args()


def probe_video(path):
    """用 ffprobe 获取视频元数据，返回 (fps, n_frames, width, height)"""
    ffprobe = find_command("ffprobe", "ffmpeg.ffprobe")
    out = subprocess.check_output([
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_streams", path
    ])
    data = json.loads(out)
    vs = next(s for s in data["streams"] if s["codec_type"] == "video")
    fps = float(Fraction(vs["r_frame_rate"]))
    n_frames = int(vs.get("nb_frames") or round(float(vs["duration"]) * fps))
    return fps, n_frames, int(vs["width"]), int(vs["height"])


def iter_raw_frames(video_path, width, height):
    """通过 ffmpeg stdout pipe 逐帧读取原始 RGB 数据"""
    ffmpeg = find_command("ffmpeg")
    frame_bytes = width * height * 3
    proc = subprocess.Popen(
        [ffmpeg, "-loglevel", "error", "-i", video_path,
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE
    )
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            yield np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()
    finally:
        proc.stdout.close()
        proc.wait()


def get_device(backend):
    """确定推理设备"""
    import torch
    if backend == "cuda":
        if not torch.cuda.is_available():
            print(f"{YELLOW}⚠ 指定了 cuda 但 CUDA 不可用，回退到 CPU{RESET}")
            return torch.device("cpu")
        return torch.device("cuda")
    elif backend == "cpu":
        return torch.device("cpu")
    else:  # auto
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")


def postprocess_alpha(alpha_np, threshold=0.3):
    """
    后处理 alpha matte:
    1. 阴影抑制：低于 threshold 的区域清零
    2. 形态学开运算：去除小碎片
    3. 最大连通区域保留：只保留面积最大的前景块
    """
    from scipy import ndimage

    h, w = alpha_np.shape

    # 1. 阴影抑制阈值 —— 将低置信度区域清零
    mask = alpha_np >= threshold

    # 2. 形态学开运算 —— 去除 <3px 的碎片
    kernel_size = max(3, int(min(h, w) * 0.005))  # 动态核大小
    if kernel_size % 2 == 0:
        kernel_size += 1
    struct = np.ones((kernel_size, kernel_size), dtype=bool)
    mask = ndimage.binary_opening(mask, structure=struct)

    # 3. 最大连通区域保留
    labeled, num_features = ndimage.label(mask)
    if num_features > 1:
        # 找到面积最大的连通区域
        sizes = ndimage.sum(mask, labeled, range(1, num_features + 1))
        largest_label = np.argmax(sizes) + 1
        mask = labeled == largest_label
    elif num_features == 0:
        # 没有前景，返回全透明
        return np.zeros_like(alpha_np)

    # 应用 mask，保留原始 alpha 的软边缘（只在通过阈值的区域内）
    result = alpha_np * mask.astype(np.float32)
    return result


def format_eta(seconds):
    """格式化剩余时间"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def main():
    args = parse_args()
    input_path = Path(args.input)
    frames_dir = Path(args.frames_dir)
    meta_path = frames_dir / "metadata.json"

    if not input_path.exists():
        sys.exit(f"{RED}✗ 错误：找不到输入文件 '{input_path}'{RESET}")

    # 检查是否已处理过
    if meta_path.exists() and not args.overwrite:
        existing = sorted(frames_dir.glob("frame_*.png"))
        if existing:
            print(f"{YELLOW}已有 {len(existing)} 帧在 '{frames_dir}/'{RESET}")
            print(f"如需重新处理，添加 {BOLD}--overwrite{RESET} 参数")
            sys.exit(0)

    frames_dir.mkdir(parents=True, exist_ok=True)

    fps, n_frames, w, h = probe_video(str(input_path))
    display_h = args.display_height
    display_w = int(round(w / h * display_h))
    print(f"{CYAN}{'─' * 50}{RESET}")
    print(f"{BOLD}视频信息{RESET}")
    print(f"  源文件：{input_path}")
    print(f"  分辨率：{w}×{h} @ {fps:.0f}fps，共 {n_frames} 帧")
    print(f"  输出尺寸：{display_w}×{display_h}px")
    print(f"{CYAN}{'─' * 50}{RESET}")

    # ── 加载 RVM 模型 ──────────────────────────────────────────────────────────
    import torch

    device = get_device(args.backend)
    device_name = "GPU (CUDA)" if device.type == "cuda" else "CPU"

    print(f"\n{BOLD}加载 RVM 模型{RESET}（{args.variant}）...")
    print(f"  推理设备：{GREEN}{device_name}{RESET}")
    print(f"  下采样比例：{args.downsample_ratio}")
    if not args.no_postprocess:
        print(f"  阴影抑制阈值：{args.alpha_threshold}")
    print(f"  首次运行会从 GitHub 下载模型权重，请耐心等待...")

    model = torch.hub.load(
        "PeterL1n/RobustVideoMatting",
        args.variant,
        trust_repo=True,
        skip_validation=True,
    )
    model = model.eval().to(device)
    print(f"{GREEN}✓ 模型加载完成{RESET}\n")

    # ── 逐帧推理 ───────────────────────────────────────────────────────────────
    print(f"{BOLD}开始处理 {n_frames} 帧...{RESET}")

    # 初始化 RVM 循环状态（时序记忆）
    rec = [None] * 4  # [r1, r2, r3, r4] 循环状态
    downsample_ratio = float(args.downsample_ratio)

    idx = 0
    t_start = time.time()

    for frame_np in iter_raw_frames(str(input_path), w, h):
        idx += 1
        out_path = frames_dir / f"frame_{idx:04d}.png"

        # numpy HWC uint8 → torch NCHW float32
        frame_tensor = torch.from_numpy(frame_np).permute(2, 0, 1).unsqueeze(0).float() / 255.0
        frame_tensor = frame_tensor.to(device)

        # RVM 推理（带循环状态）
        with torch.no_grad():
            fgr, pha, *rec = model(frame_tensor, *rec, downsample_ratio)

        # alpha matte: [1,1,H,W] → [H,W] numpy
        alpha = pha[0, 0].cpu().numpy()

        # 后处理
        if not args.no_postprocess:
            alpha = postprocess_alpha(alpha, threshold=args.alpha_threshold)

        # 合成 RGBA 输出
        # 使用前景估计 (fgr) 而非原始帧，RVM 的前景估计更干净
        fgr_np = fgr[0].permute(1, 2, 0).cpu().numpy()  # [H,W,3] float 0~1
        fgr_np = np.clip(fgr_np * 255, 0, 255).astype(np.uint8)
        alpha_uint8 = np.clip(alpha * 255, 0, 255).astype(np.uint8)

        rgba = np.dstack([fgr_np, alpha_uint8])
        pil_rgba = Image.fromarray(rgba, "RGBA")
        pil_rgba_scaled = pil_rgba.resize((display_w, display_h), Image.LANCZOS)
        pil_rgba_scaled.save(str(out_path), "PNG")

        # 进度显示
        elapsed = time.time() - t_start
        speed = idx / elapsed if elapsed > 0 else 0
        eta = (n_frames - idx) / speed if speed > 0 else 0
        pct = idx / n_frames * 100

        bar_len = 30
        filled = int(bar_len * idx / n_frames)
        bar = f"{'█' * filled}{'░' * (bar_len - filled)}"
        status = (
            f"\r  {GREEN}{bar}{RESET} "
            f"{pct:5.1f}% "
            f"[{idx}/{n_frames}] "
            f"{DIM}{speed:.1f} fps | ETA {format_eta(eta)}{RESET}"
        )
        sys.stdout.write(status)
        sys.stdout.flush()

    sys.stdout.write("\n")  # 换行

    # ── 写入 metadata ──────────────────────────────────────────────────────────
    sample = Image.open(frames_dir / "frame_0001.png")
    actual_w, actual_h = sample.size

    meta = {
        "fps": fps,
        "frame_count": idx,
        "width": actual_w,
        "height": actual_h,
        "source_video": str(input_path),
        "model": f"RVM-{args.variant}",
        "downsample_ratio": args.downsample_ratio,
        "alpha_threshold": args.alpha_threshold if not args.no_postprocess else None,
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    elapsed_total = time.time() - t_start
    print(f"\n{GREEN}{'─' * 50}{RESET}")
    print(f"{GREEN}✓ 完成！{RESET}{idx} 帧已写入 '{frames_dir}/'")
    print(f"  帧尺寸：{actual_w}×{actual_h}px")
    print(f"  用时：{format_eta(elapsed_total)}（{idx / elapsed_total:.1f} fps）")
    print(f"  元数据：{meta_path}")
    print(f"\n{CYAN}下一步运行：{RESET}{BOLD}python dancer.py{RESET}")


if __name__ == "__main__":
    main()
