"""Lomb–Scargle 计算子进程。

每批任务由父进程(FastAPI 主进程)拉起两个本脚本实例:
  * worker 0:计算原始光变曲线的功率谱 + 100 次置换的最大功率;
  * worker 1:只计算另外 100 次置换的最大功率。

输入为父进程写好的临时 CSV(time_day,flux 两列,已校验),
结果以 JSON 原子写入指定临时文件,由父进程汇总。
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
from astropy.timeseries import LombScargle


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Lomb-Scargle batch worker")
    ap.add_argument("--input", required=True, help="输入 CSV(time_day,flux)")
    ap.add_argument("--output", required=True, help="结果 JSON 输出路径")
    ap.add_argument("--fmin", type=float, required=True, help="频率下限 = 1/周期上限")
    ap.add_argument("--fmax", type=float, required=True, help="频率上限 = 1/周期下限")
    ap.add_argument("--nfreq", type=int, required=True, help="频率网格点数")
    ap.add_argument("--nperm", type=int, required=True, help="本子进程承担的置换次数")
    ap.add_argument("--seed", type=int, required=True, help="置换随机种子")
    ap.add_argument("--compute-original", action="store_true",
                    help="同时计算原始序列的功率谱")
    return ap.parse_args()


def ls_power_max(t: np.ndarray, y: np.ndarray, freq: np.ndarray) -> float:
    """带浮动均值的 Lomb–Scargle,返回全频段最大功率。"""
    ls = LombScargle(t, y, fit_mean=True, center_data=True,
                     normalization="standard")
    return float(np.max(ls.power(freq, method="fast")))


def main() -> None:
    args = parse_args()

    data = np.loadtxt(args.input, delimiter=",", skiprows=1, ndmin=2)
    t = np.ascontiguousarray(data[:, 0], dtype=float)
    y = np.ascontiguousarray(data[:, 1], dtype=float)

    # 频率范围取周期范围的倒数,均匀取 nfreq 点
    freq = np.linspace(args.fmin, args.fmax, args.nfreq)

    out: dict = {"seed": args.seed, "nperm": args.nperm}

    if args.compute_original:
        ls = LombScargle(t, y, fit_mean=True, center_data=True,
                         normalization="standard")
        out["orig_power"] = ls.power(freq, method="fast").tolist()

    # 固定时刻,随机重排光通量,每次重算全频段最大功率
    rng = np.random.default_rng(args.seed)
    perm_max = []
    for _ in range(args.nperm):
        y_perm = rng.permutation(y)
        perm_max.append(ls_power_max(t, y_perm, freq))
    out["perm_max"] = perm_max

    # 原子写:先写临时文件再改名,避免父进程读到半截 JSON
    tmp_path = args.output + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    os.replace(tmp_path, args.output)


if __name__ == "__main__":
    main()
