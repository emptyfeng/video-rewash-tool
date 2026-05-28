#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
  短视频大批量"去重·洗片·抹除千川盲水印" 桌面工具
  —— FFmpeg 核心引擎 + Tkinter 可视化界面
==============================================================================
  功能特点：
    1. 智能 FFmpeg 路径检测（同目录优先 → 环境变量兜底）
    2. 无感 N 卡加速测试（h264_nvenc 虚拟探测 → 自动回退 CPU）
    3. Tkinter 傻瓜式界面（选文件夹 + 一键开始 + 实时日志）
    4. 多线程并发处理（固定 2 路，防小白机器熔断）
    5. 参数动态随机化（缩放 / 旋转 / 噪点 / 光影 / 变速）
    6. 工业级 try-catch，单条损坏不卡死，绝无闪退
==============================================================================
  打包命令（Windows）：
      pip install pyinstaller
      pyinstaller --noconsole --onefile --icon=icon.ico video_rewash.py
==============================================================================
"""

import os
import sys
import re
import json
import math
import random
import shutil
import queue
import threading
import subprocess as sp
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ──────────────────────────────────────────────────────────────────────────────
#  Tkinter 导入（无第三方依赖，Python 自带）
# ──────────────────────────────────────────────────────────────────────────────
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    # 极低概率的兼容兜底——打印错误但不崩溃
    print("【严重错误】当前 Python 环境缺少 tkinter 模块，无法启动图形界面。")
    print("请运行: sudo apt-get install python3-tk   (Linux)")
    print("或重新安装带 tkinter 的 Python 发行版 (Windows/macOS)")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
#  全局常量与配置
# ══════════════════════════════════════════════════════════════════════════════

APP_NAME = "视频去重冲洗工具 v2.0"
MAX_WORKERS = 2  # 最大并发数（小白友好，防止显卡/CPU过载）

# 支持的视频扩展名（不区分大小写）
SUPPORTED_EXTS = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.ts', '.m4v'}

# 默认文件夹名
DEFAULT_INPUT_DIR  = "input_videos"
DEFAULT_OUTPUT_DIR = "output_videos"


# ══════════════════════════════════════════════════════════════════════════════
#  核心功能模块
# ══════════════════════════════════════════════════════════════════════════════

def detect_ffmpeg() -> str:
    """
    【智能化 FFmpeg 路径检测】
    优先级：exe 同级目录  →  系统环境变量 PATH
    返回 ffmpeg 可执行文件路径，找不到则返回 None。
    """
    # ── 1. 检测程序自身所在目录是否有 ffmpeg.exe ──
    #     兼容 PyInstaller 打包后的 _MEIPASS 和普通脚本
    candidates = []

    # 如果是 PyInstaller 打包的 exe
    if hasattr(sys, '_MEIPASS'):
        base = Path(sys._MEIPASS)
        candidates.append(base / "ffmpeg.exe")
        candidates.append(base / "ffmpeg")

    # 脚本所在目录
    script_dir = Path(getattr(sys, 'argv', [''])[0]).parent
    candidates.append(script_dir / "ffmpeg.exe")
    candidates.append(script_dir / "ffmpeg")

    # 当前工作目录
    cwd = Path.cwd()
    candidates.append(cwd / "ffmpeg.exe")
    candidates.append(cwd / "ffmpeg")

    for cand in candidates:
        if cand.exists() and cand.is_file():
            return str(cand.resolve())

    # ── 2. 环境变量 PATH 中查找 ──
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    # ── 3. 真的找不到了 ──
    return None


def test_nvenc(ffmpeg_path: str) -> bool:
    """
    【无感 N 卡加速测试】
    用 h264_nvenc 对一个 1 帧虚拟数据进行极速编码测试。
    成功 → True（启用硬件加速），失败 → False（回退 CPU）。
    整个过程在内存中完成，不产生任何临时文件。
    """
    try:
        cmd = [
            ffmpeg_path,
            "-f", "lavfi",                  # 虚拟输入源
            "-i", "color=c=black:s=64x64:d=0.04",  # 1 帧黑屏（< 0.04 秒）
            "-c:v", "h264_nvenc",
            "-preset", "p1",
            "-t", "0.04",
            "-f", "null",                   # 不写文件，仅测试编码器可用性
            "-y",
            "NUL" if os.name == "nt" else "/dev/null"
        ]
        result = sp.run(cmd, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def build_filter_string(scale: float, rotate_deg: float, noise_pct: float,
                        contrast: float, brightness: float) -> str:
    """
    构建 FFmpeg 滤镜链（核心去重算法）
    参数说明：
        scale       : 画面中心放大倍数 (1.02 ~ 1.05)
        rotate_deg  : 顺时针旋转角度 (0.3 ~ 0.8 °)
        noise_pct   : 像素级噪点强度 (1.5% ~ 2.5%)
        contrast    : 对比度系数 (1.01 ~ 1.03)
        brightness  : 亮度偏移量 (0.01 ~ 0.02)
    """
    # 放大 → 旋转（裁剪填满边界）→ 噪点 → 对比度+亮度 → 像素格式
    # 注意：滤镜顺序影响最终视觉/去重效果
    filters = []

    # ── 1. 画面中心缩放（用 scale 然后 pad 或直接用 scale 保持原分辨率） ──
    #     zoompan 更灵活但更慢；这里用 scale + setdar/setsar 保持比例
    #     直接使用 scale=iw*ZOOM:ih*ZOOM，然后裁剪回原分辨率
    filters.append(
        f"scale=iw*{scale:.4f}:ih*{scale:.4f}:flags=bicubic,"
        f"crop=iw/{scale:.4f}:ih/{scale:.4f}"
    )

    # ── 2. 画面轻微旋转 ──
    #     rotate 滤镜：顺时针角度（弧度），自动黑色背景，ow=iw:oh=ih 裁剪填满
    rad = math.radians(rotate_deg)
    filters.append(
        f"rotate={rad:.6f}:ow=iw:oh=ih:c=none"
    )

    # ── 3. 像素级动态噪点（打碎盲水印核心） ──
    #     noise 滤镜：alls 控制所有通道强度，c0s/c1s/c2s/c3s 分别控制各分量
    #     noise_pct 转化为 0~255 范围的强度值
    noise_strength = int(noise_pct * 255 / 100)
    filters.append(
        f"noise=alls={noise_strength}:allu={noise_strength}"
    )

    # ── 4. 光影微调（对比度 + 亮度） ──
    #     eq 滤镜：contrast/brightness
    filters.append(
        f"eq=contrast={contrast:.4f}:brightness={brightness:.4f}"
    )

    # ── 5. 像素格式标准化 ──
    filters.append("format=yuv420p")

    return ",".join(filters)


def process_single_video(
    input_path: str,
    output_path: str,
    ffmpeg_path: str,
    use_nvenc: bool,
    speed: float,
    log_queue: queue.Queue,
) -> bool:
    """
    【单条视频冲洗任务】
    返回 True 表示处理成功，False 表示失败（已跳过）。
    log_queue 用于线程安全地将日志发回主界面。
    """
    video_name = Path(input_path).name
    try:
        # ── 动态生成随机参数（每条视频不同，打破风控模板） ──
        scale       = random.uniform(1.02, 1.05)
        rotate_deg  = random.uniform(0.3, 0.8)
        noise_pct   = random.uniform(1.5, 2.5)
        contrast    = random.uniform(1.01, 1.03)
        brightness  = random.uniform(0.01, 0.02)
        # speed 由外部传入，保证音视频同步

        # ── 构建滤镜链 ──
        # 将视频处理滤镜（缩放/旋转/噪点/光影）与变速整合到同一个 filter_complex 中
        vf_chain = build_filter_string(scale, rotate_deg, noise_pct, contrast, brightness)

        # ── 构建 FFmpeg 命令行 ──
        if use_nvenc:
            vcodec = "h264_nvenc"
            vcodec_opts = ["-preset", "p1", "-rc", "vbr", "-cq", "23"]
            accel_tag = "N卡加速"
        else:
            vcodec = "libx264"
            vcodec_opts = ["-preset", "medium", "-crf", "23"]
            accel_tag = "CPU编码"

        cmd = [ffmpeg_path]

        # 输入
        cmd.extend(["-i", input_path])

        # 视频滤镜（缩放/旋转/噪点/光影）+ 变速 + 音频变速，全部整合到 filter_complex
        # 注意：speed 范围 0.985~1.015，单次 atempo 即可（不超 2.0 限制）
        filter_complex_str = (
            f"[0:v]{vf_chain},setpts={1/speed:.6f}*PTS[vout];"
            f"[0:a]atempo={speed:.6f}[aout]"
        )
        cmd.extend(["-filter_complex", filter_complex_str])
        cmd.extend(["-map", "[vout]", "-map", "[aout]"])

        # 视频编码
        cmd.extend(["-c:v", vcodec] + vcodec_opts)

        # 音频编码
        cmd.extend(["-c:a", "aac", "-b:a", "128k"])

        # 抹除元数据
        cmd.extend(["-map_metadata", "-1", "-map_chapters", "-1"])

        # 像素格式与输出
        cmd.extend(["-pix_fmt", "yuv420p"])

        # 覆盖输出
        cmd.extend(["-y", output_path])

        # ── 执行 ──
        log_queue.put(f"[处理中] {video_name}  (缩放×{scale:.3f} / 旋转{rotate_deg:.2f}° / "
                      f"噪点{noise_pct:.1f}% / 对比度{contrast:.3f} / 亮度{brightness:.3f} / "
                      f"变速×{speed:.4f})")

        result = sp.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,   # 单条最长 10 分钟
        )

        if result.returncode == 0:
            log_queue.put(f"  ✅ 【成功】{video_name} 已彻底去重（{accel_tag}）")
            return True
        else:
            # 截取 FFmpeg 错误信息的最后几行
            err_lines = result.stderr.strip().splitlines()
            err_short = " | ".join(err_lines[-5:]) if err_lines else "未知错误"
            log_queue.put(f"  ❌ 【失败】{video_name} → {err_short[:150]}")
            return False

    except sp.TimeoutExpired:
        log_queue.put(f"  ⏰ 【超时】{video_name} 处理时间超过10分钟，已跳过")
        return False
    except Exception as e:
        log_queue.put(f"  💥 【异常】{video_name} → {str(e)[:120]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  Tkinter 主界面类
# ══════════════════════════════════════════════════════════════════════════════

class VideoRewashApp:
    """视频去重冲洗工具主窗口"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("820x680")
        self.root.minsize(700, 580)

        # ── 尝试设置图标（如果有的话，没有也完全不报错） ──
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        # ── FFmpeg 路径检测（程序启动时立即执行） ──
        self.ffmpeg_path = detect_ffmpeg()
        self.use_nvenc = False  # 稍后测试后决定
        self._processing = False  # 是否正在处理中（防止重复点击）
        self._stop_flag = False   # 停止信号

        # ── 默认路径 ──
        # 使用 exe/脚本自身所在目录，避免 cwd 跑到 C:\Windows\system32 报权限错误
        if getattr(sys, 'frozen', False):
            # PyInstaller 打包后的 exe
            exe_dir = Path(sys.executable).parent
        else:
            # 普通 Python 脚本
            exe_dir = Path.cwd()
        
        self.input_dir  = exe_dir / DEFAULT_INPUT_DIR
        self.output_dir = exe_dir / DEFAULT_OUTPUT_DIR
        # 创建目录，失败不崩溃（用户可在界面上重新选路径）
        try:
            self.input_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.input_dir = exe_dir / DEFAULT_INPUT_DIR  # 重置，不创建
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.output_dir = exe_dir / DEFAULT_OUTPUT_DIR

        # ── 日志队列（线程安全地回传日志到主线程） ──
        self.log_queue = queue.Queue()

        # ── 构建界面 ──
        self._build_ui()

        # ── FFmpeg 检查结果 ──
        self._check_ffmpeg()

        # ── 启动日志轮询器 ──
        self._poll_log_queue()

        # ── 窗口关闭事件（确保线程安全退出） ──
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ──────────────────────────────────────────────────────────────────────────
    #  界面构建
    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        """构建完整的 Tkinter 界面"""
        # ── 样式 ──
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")

        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ── 标题 ──
        title_label = ttk.Label(
            main_frame,
            text="🎬 短视频去重冲洗工具",
            font=("微软雅黑", 16, "bold"),
            foreground="#1a73e8",
        )
        title_label.pack(pady=(0, 10))

        # ── 状态条（FFmpeg / 显卡检测结果） ──
        self.status_var = tk.StringVar(value="正在检测环境...")
        status_bar = ttk.Label(
            main_frame,
            textvariable=self.status_var,
            font=("微软雅黑", 9),
            foreground="#555",
            relief=tk.SUNKEN,
        )
        status_bar.pack(fill=tk.X, pady=(0, 6))

        # ── 输入输出路径选择区 ──
        path_frame = ttk.LabelFrame(main_frame, text=" 文件夹选择 ", padding=8)
        path_frame.pack(fill=tk.X, pady=4)

        # 输入文件夹
        ttk.Label(path_frame, text="📥 输入文件夹：").grid(row=0, column=0, sticky=tk.W, padx=4, pady=3)
        self.input_var = tk.StringVar(value=str(self.input_dir))
        input_entry = ttk.Entry(path_frame, textvariable=self.input_var, width=55)
        input_entry.grid(row=0, column=1, padx=4, pady=3, sticky=tk.EW)
        ttk.Button(path_frame, text="浏览...", command=self._browse_input).grid(row=0, column=2, padx=4, pady=3)

        # 输出文件夹
        ttk.Label(path_frame, text="📤 输出文件夹：").grid(row=1, column=0, sticky=tk.W, padx=4, pady=3)
        self.output_var = tk.StringVar(value=str(self.output_dir))
        output_entry = ttk.Entry(path_frame, textvariable=self.output_var, width=55)
        output_entry.grid(row=1, column=1, padx=4, pady=3, sticky=tk.EW)
        ttk.Button(path_frame, text="浏览...", command=self._browse_output).grid(row=1, column=2, padx=4, pady=3)

        path_frame.columnconfigure(1, weight=1)

        # ── 操作按钮区 ──
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=8)

        self.start_btn = ttk.Button(
            btn_frame,
            text="🚀 一键开始冲洗",
            command=self._start_processing,
            style="Accent.TButton" if hasattr(style, 'theme_use') and 'vista' in style.theme_names() else None,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn = ttk.Button(
            btn_frame,
            text="⏹ 停止",
            command=self._stop_processing,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))

        # 打开输出文件夹按钮
        ttk.Button(
            btn_frame,
            text="📂 打开输出文件夹",
            command=self._open_output_dir,
        ).pack(side=tk.RIGHT)

        # ── 进度条 ──
        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=2)
        ttk.Label(progress_frame, text="进度：").pack(side=tk.LEFT)
        self.progress_var = tk.IntVar(value=0)
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            length=500,
        )
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.progress_label = ttk.Label(progress_frame, text="0 / 0")
        self.progress_label.pack(side=tk.LEFT)

        # ── 日志框（带滚动条） ──
        log_frame = ttk.LabelFrame(main_frame, text=" 处理日志 ", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=4)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            state=tk.NORMAL,
            height=14,
            relief=tk.SUNKEN,
            borderwidth=2,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 日志框右键菜单
        self._setup_log_context_menu()

        # 初始提示
        self._log("=" * 60)
        self._log(f"  {APP_NAME} 已启动")
        self._log(f"  输入目录: {self.input_var.get()}")
        self._log(f"  输出目录: {self.output_var.get()}")
        self._log("=" * 60)

    def _setup_log_context_menu(self):
        """日志框右键菜单：清空 / 全选 / 复制"""
        menu = tk.Menu(self.log_text, tearoff=0)
        menu.add_command(label="清空日志", command=lambda: self.log_text.delete("1.0", tk.END))
        menu.add_command(label="全选",    command=lambda: self.log_text.tag_add(tk.SEL, "1.0", tk.END))
        menu.add_command(label="复制",    command=lambda: self.root.clipboard_append(
            self.log_text.get(tk.SEL_FIRST, tk.SEL_LAST) if self.log_text.tag_ranges(tk.SEL)
            else self.log_text.get("1.0", tk.END)
        ))
        self.log_text.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    # ──────────────────────────────────────────────────────────────────────────
    #  文件夹选择
    # ──────────────────────────────────────────────────────────────────────────

    def _browse_input(self):
        """选择输入文件夹"""
        path = filedialog.askdirectory(title="选择输入文件夹（放视频的目录）")
        if path:
            self.input_var.set(path)
            self._log(f"📂 输入目录已切换至: {path}")

    def _browse_output(self):
        """选择输出文件夹"""
        path = filedialog.askdirectory(title="选择输出文件夹（洗好的视频放这里）")
        if path:
            self.output_var.set(path)
            self._log(f"📂 输出目录已切换至: {path}")

    def _open_output_dir(self):
        """打开输出文件夹（跨平台）"""
        out_path = Path(self.output_var.get())
        if out_path.exists():
            try:
                if os.name == "nt":
                    os.startfile(str(out_path))  # Windows
                elif sys.platform == "darwin":
                    sp.run(["open", str(out_path)])
                else:
                    sp.run(["xdg-open", str(out_path)])
            except Exception as e:
                self._log(f"⚠ 无法打开文件夹: {e}")
        else:
            self._log(f"⚠ 输出目录不存在: {out_path}")

    # ──────────────────────────────────────────────────────────────────────────
    #  环境检测
    # ──────────────────────────────────────────────────────────────────────────

    def _check_ffmpeg(self):
        """
        全面环境检查：
        1) FFmpeg 是否存在
        2) 如果存在，测试 h264_nvenc 是否可用
        """
        if not self.ffmpeg_path:
            messagebox.showerror(
                "缺少 FFmpeg",
                "未找到 ffmpeg.exe 核心组件，请将该组件放入软件文件夹中！"
            )
            self.status_var.set("❌ 未找到 FFmpeg，功能不可用")
            self.start_btn.config(state=tk.DISABLED)
            self._log("【严重】未找到 FFmpeg！请将 ffmpeg.exe 放在程序同目录下")
            return

        # FFmpeg 已找到
        self.status_var.set(f"✅ FFmpeg: {Path(self.ffmpeg_path).name}")

        # ── 无感 NVENC 测试 ──
        self._log("🔍 正在后台检测显卡加速能力...")
        self.root.update()
        has_nvenc = test_nvenc(self.ffmpeg_path)

        if has_nvenc:
            self.use_nvenc = True
            self.status_var.set("✅ FFmpeg OK | 🟢 N 卡硬件加速已启用")
            self._log("  ✅ 检测到 NVIDIA 显卡，已启用硬件加速（h264_nvenc）")
        else:
            self.use_nvenc = False
            self.status_var.set("✅ FFmpeg OK | 🟡 CPU 编码模式（未检测到 N 卡加速）")
            self._log("  ⚠ 未检测到 NVIDIA 硬件加速，将自动使用 CPU 编码（libx264）")
            self._log("  ℹ 如果你的电脑是 NVIDIA 显卡，请检查驱动是否正确安装")

    # ──────────────────────────────────────────────────────────────────────────
    #  日志管理（线程安全）
    # ──────────────────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        """在主线程中向日志框追加一条消息（含时间戳）"""
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
            self.log_text.see(tk.END)
        except Exception:
            pass  # 窗口已销毁时忽略

    def _poll_log_queue(self):
        """
        定时轮询日志队列，将工作线程中的日志安全地插入主界面。
        每 100ms 检查一次，确保界面永不阻塞。
        """
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._log(msg)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_log_queue)

    # ──────────────────────────────────────────────────────────────────────────
    #  核心处理逻辑
    # ──────────────────────────────────────────────────────────────────────────

    def _start_processing(self):
        """启动批量冲洗任务（按钮回调）"""
        # ── 防止重复点击 ──
        if self._processing:
            self._log("⚠ 正在处理中，请耐心等待...")
            return

        # ── 确保 FFmpeg 可用 ──
        if not self.ffmpeg_path:
            messagebox.showerror("缺少组件", "未找到 FFmpeg，无法开始处理")
            return

        # ── 扫描输入目录 ──
        input_dir = Path(self.input_var.get())
        output_dir = Path(self.output_var.get())

        if not input_dir.exists():
            messagebox.showerror("目录错误", f"输入文件夹不存在：\n{input_dir}")
            return

        # 确保输出目录存在
        output_dir.mkdir(parents=True, exist_ok=True)

        # 收集所有视频文件
        video_files = []
        for f in sorted(input_dir.iterdir()):
            if f.suffix.lower() in SUPPORTED_EXTS and f.is_file():
                video_files.append(f)

        if not video_files:
            messagebox.showinfo("没有视频", f"输入文件夹中未找到视频文件：\n{input_dir}")
            return

        # ── 更新界面状态 ──
        self._processing = True
        self._stop_flag = False
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        total = len(video_files)
        self.progress_var.set(0)
        self.progress_bar.config(maximum=total)
        self.progress_label.config(text=f"0 / {total}")

        self._log("=" * 60)
        self._log(f"🚀 批量冲洗开始！共发现 {total} 个视频")
        self._log(f"   编码模式: {'N卡硬件加速 (h264_nvenc)' if self.use_nvenc else 'CPU软件编码 (libx264)'}")
        self._log(f"   并发数: {MAX_WORKERS} 路")
        self._log("=" * 60)

        # ── 启动后台处理线程 ──
        self._processing_thread = threading.Thread(
            target=self._run_batch,
            args=(video_files, output_dir),
            daemon=True,
        )
        self._processing_thread.start()

    def _run_batch(self, video_files: list, output_dir: Path):
        """
        后台批量处理线程（运行在独立线程中，不会阻塞界面）
        使用 ThreadPoolExecutor 控制并发数
        """
        total = len(video_files)
        completed = 0
        success_count = 0
        fail_count = 0

        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                # 提交所有任务
                future_map = {}
                for i, video_path in enumerate(video_files):
                    # 生成输出路径
                    stem = video_path.stem
                    # 如果存在同名，加随机后缀避免覆盖
                    out_name = f"{stem}_冲洗版.mp4"
                    out_path = output_dir / out_name
                    counter = 1
                    while out_path.exists():
                        out_name = f"{stem}_冲洗版_{counter}.mp4"
                        out_path = output_dir / out_name
                        counter += 1

                    # 动态生成速度参数（每条视频不同）
                    speed = random.uniform(0.985, 1.015)

                    # 提交到线程池
                    future = executor.submit(
                        process_single_video,
                        str(video_path),
                        str(out_path),
                        self.ffmpeg_path,
                        self.use_nvenc,
                        speed,
                        self.log_queue,
                    )
                    future_map[future] = (i + 1, video_path.name)

                # 收集结果
                for future in as_completed(future_map):
                    idx, name = future_map[future]
                    completed += 1

                    # 检查停止信号
                    if self._stop_flag:
                        self.log_queue.put(f"  ⏹ 已收到停止信号，跳过剩余 {total - completed} 个视频")
                        break

                    try:
                        ok = future.result()
                        if ok:
                            success_count += 1
                        else:
                            fail_count += 1
                    except Exception as e:
                        fail_count += 1
                        self.log_queue.put(f"  💥 任务异常: {name} → {str(e)[:80]}")

                    # 更新进度（线程安全：通过 root.after 回到主线程）
                    self.root.after(0, self._update_progress, completed, total)

            # ── 汇总报告 ──
            self.log_queue.put("=" * 60)
            if self._stop_flag:
                self.log_queue.put(f"⏹ 已手动停止。成功: {success_count}, 失败: {fail_count}, 总计: {completed}/{total}")
            else:
                self.log_queue.put(f"🎉 全部处理完成！")
                self.log_queue.put(f"    ✅ 成功: {success_count} 个   ❌ 失败: {fail_count} 个")
                self.log_queue.put(f"    📁 输出目录: {output_dir}")
            self.log_queue.put("=" * 60)

        except Exception as e:
            self.log_queue.put(f"🔥 批量处理异常: {str(e)[:120]}")

        finally:
            # 恢复界面状态
            self.root.after(0, self._reset_ui)

    def _update_progress(self, current: int, total: int):
        """更新进度条（由主线程执行）"""
        self.progress_var.set(current)
        self.progress_label.config(text=f"{current} / {total}")

    def _reset_ui(self):
        """处理后恢复界面可用状态"""
        self._processing = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    # ──────────────────────────────────────────────────────────────────────────
    #  停止控制
    # ──────────────────────────────────────────────────────────────────────────

    def _stop_processing(self):
        """安全停止正在进行的批量处理"""
        if self._processing:
            self._stop_flag = True
            self._log("⏹ 正在向处理引擎发送停止信号...")
            self.stop_btn.config(state=tk.DISABLED)

    # ──────────────────────────────────────────────────────────────────────────
    #  窗口关闭
    # ──────────────────────────────────────────────────────────────────────────

    def _on_close(self):
        """窗口关闭时安全退出"""
        if self._processing:
            if not messagebox.askokcancel("确认退出", "正在处理视频，确认退出？\n未完成的任务将中断。"):
                return
            self._stop_flag = True
            # 给线程一点时间优雅退出
            if hasattr(self, '_processing_thread') and self._processing_thread.is_alive():
                self._processing_thread.join(timeout=2)
        try:
            self.root.destroy()
        except Exception:
            sys.exit(0)


# ══════════════════════════════════════════════════════════════════════════════
#  程序入口
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """启动 Tkinter 主循环"""
    root = tk.Tk()
    app = VideoRewashApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\n用户中断，程序退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
