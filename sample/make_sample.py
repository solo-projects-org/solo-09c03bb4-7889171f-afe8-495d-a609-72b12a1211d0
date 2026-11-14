"""生成带周期信号的虚构样例测光序列(sample_star.csv)。

虚构目标:一颗周期约 4.25 天的脉动变星,正弦信号叠加少量噪声,
采样间隔模拟巡测的不规则观测。固定随机种子,结果可复现。
"""
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent / "sample_star.csv"

rng = np.random.default_rng(20261114)

# 约 220 天内的 160 次不规则观测(时刻互异)
n = 160
t = np.sort(rng.uniform(0.0, 220.0, size=n))

period = 4.25          # 真实周期(天)
phi = 0.7
flux = (10.0
        + 0.55 * np.sin(2 * np.pi * t / period + phi)
        + 0.18 * np.sin(4 * np.pi * t / period)   # 一点二次谐波,更像真星
        + rng.normal(0.0, 0.12, size=n))          # 测光噪声

with OUT.open("w", encoding="utf-8") as fh:
    fh.write("time_day,flux\n")
    for ti, fi in zip(t, flux):
        fh.write(f"{ti:.6f},{fi:.6f}\n")

print(f"已写出 {OUT}({n} 行,真周期 {period} 天)")
