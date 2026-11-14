"""周期变星候选筛选 —— FastAPI 主程序。

功能
----
上传单颗星的 time_day、flux 两列 CSV(至少 10 行、值有限、时刻互异),
填写 0<下限<上限 的周期范围;取倒数得频率范围,均匀取 10000 点,
用带浮动均值的 Lomb–Scargle 计算功率谱,给出最高峰周期、功率谱图、
按该周期折叠的相位散点图;固定时刻随机重排光通量 200 次,每次重算
全频段最大功率,给出置换尾概率 (不低于原峰的次数+1)/201。

并发与生命周期
--------------
* 每批任务使用两个计算子进程,最多两批并行,超额拒绝(HTTP 429);
* 任务可查询、可取消;
* 无论成功、失败还是取消,都必须等本批全部计算子进程退出、临时文件
  清理完毕,才公布结局并释放名额;另一批不受影响;
* 取消一经受理,不再发布任何迟到结果;重复取消不会重复释放名额;
* 全部状态只保存在内存中。
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import secrets
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无显示环境下出图

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

APP_DIR = Path(__file__).resolve().parent
WORKER_PY = APP_DIR / "worker.py"
INDEX_HTML = APP_DIR / "static" / "index.html"
SAMPLE_CSV = APP_DIR.parent / "sample" / "sample_star.csv"

N_FREQ = 10_000          # 频率网格点数
N_PERM = 200             # 置换检验次数
WORKERS_PER_JOB = 2      # 每批计算子进程数
MAX_PARALLEL_JOBS = 2    # 最多并行批数
MIN_ROWS = 10            # CSV 最少行数
MAX_ROWS = 50_000        # CSV 最多行数(防御性上限)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_KEPT_JOBS = 64       # 内存中最多保留的任务记录数
TERMINAL_STATES = ("done", "failed", "cancelled")

# 全部任务状态只存内存
JOBS: dict[str, "Job"] = {}


class Job:
    """一批计算任务的全部内存态。"""

    def __init__(self, period_min: float, period_max: float,
                 n_rows: int, filename: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.state = "running"  # running / done / failed / cancelled
        self.cancel_requested = False
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.period_min = period_min
        self.period_max = period_max
        self.n_rows = n_rows
        self.filename = filename
        self.error: str | None = None
        self.result: dict | None = None  # 指标 + PNG 字节
        self.tmpdir = Path(tempfile.mkdtemp(prefix=f"lsjob_{self.id}_"))
        self.procs: list[asyncio.subprocess.Process] = []

    def summary(self) -> dict:
        d = {
            "job_id": self.id,
            "state": self.state,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "period_min": self.period_min,
            "period_max": self.period_max,
            "n_rows": self.n_rows,
            "filename": self.filename,
            "error": self.error,
        }
        if self.state == "done" and self.result:
            d["metrics"] = {k: v for k, v in self.result.items()
                            if not k.endswith("_png")}
        return d


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #

def validate_csv(raw: bytes) -> tuple[np.ndarray, np.ndarray]:
    """校验并解析 CSV,返回 (time_day, flux) 两个等长数组。"""
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(400, f"CSV 解析失败:{exc}")
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in ("time_day", "flux") if c not in df.columns]
    if missing:
        raise HTTPException(400, f"CSV 缺少必需列:{', '.join(missing)}"
                                 "(需要 time_day、flux 两列)")
    if len(df) < MIN_ROWS:
        raise HTTPException(400, f"至少需要 {MIN_ROWS} 行数据,当前 {len(df)} 行")
    if len(df) > MAX_ROWS:
        raise HTTPException(400, f"数据行数超过上限 {MAX_ROWS}")

    t = pd.to_numeric(df["time_day"], errors="coerce").to_numpy(dtype=float)
    y = pd.to_numeric(df["flux"], errors="coerce").to_numpy(dtype=float)
    if not (np.all(np.isfinite(t)) and np.all(np.isfinite(y))):
        raise HTTPException(400, "time_day、flux 必须全部为有限数值"
                                 "(不允许空值/NaN/Inf)")
    if np.unique(t).size != t.size:
        raise HTTPException(400, "观测时刻必须互异(存在重复时刻)")
    return t, y


def validate_period_range(period_min: float, period_max: float) -> None:
    if not (math.isfinite(period_min) and math.isfinite(period_max)):
        raise HTTPException(400, "周期上下限必须为有限数值")
    if not (0.0 < period_min < period_max):
        raise HTTPException(400, "周期范围须满足 0 < 下限 < 上限")


# --------------------------------------------------------------------------- #
# 绘图
# --------------------------------------------------------------------------- #

def fig_to_png(fig: plt.Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def make_power_plot(periods: np.ndarray, power: np.ndarray,
                    best_period: float, max_power: float,
                    pmin: float, pmax: float) -> bytes:
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    ax.plot(periods, power, lw=0.8, color="#1f5fa8")
    ax.axvline(best_period, color="#d62728", lw=1.0, ls="--",
               label=f"peak P = {best_period:.6g} d")
    ax.plot([best_period], [max_power], "o", color="#d62728", ms=5)
    ax.set_xlim(pmin, pmax)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Period (day)")
    ax.set_ylabel("Lomb-Scargle power")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.25)
    return fig_to_png(fig)


def make_phase_plot(phase: np.ndarray, flux: np.ndarray,
                    best_period: float) -> bytes:
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    # 画两个相位周期便于观察
    ax.plot(np.concatenate([phase, phase + 1.0]),
            np.concatenate([flux, flux]), "o", ms=3.5,
            color="#1f5fa8", alpha=0.75)
    ax.set_xlim(0, 2)
    ax.set_xlabel(f"Phase (folded at P = {best_period:.6g} d)")
    ax.set_ylabel("Flux")
    ax.grid(alpha=0.25)
    return fig_to_png(fig)


# --------------------------------------------------------------------------- #
# 结果汇总
# --------------------------------------------------------------------------- #

def combine_results(job: Job) -> dict:
    """读取两个子进程的输出,合并置换结果并出图。"""
    tmp = job.tmpdir
    outputs = []
    for k in range(WORKERS_PER_JOB):
        out_file = tmp / f"out_{k}.json"
        if not out_file.exists():
            raise RuntimeError(f"子进程 {k} 未产生输出文件")
        outputs.append(json.loads(out_file.read_text(encoding="utf-8")))

    # worker 0 负责计算原始序列功率谱
    orig_power = np.asarray(outputs[0]["orig_power"], dtype=float)
    perm_max = np.asarray(
        outputs[0]["perm_max"] + outputs[1]["perm_max"], dtype=float)
    if perm_max.size != N_PERM:
        raise RuntimeError(f"置换次数异常:期望 {N_PERM},实得 {perm_max.size}")

    # 与 worker 完全一致的频率网格
    freq = np.linspace(1.0 / job.period_max, 1.0 / job.period_min, N_FREQ)
    i_best = int(np.argmax(orig_power))
    best_period = float(1.0 / freq[i_best])
    max_power = float(orig_power[i_best])

    # 置换尾概率:(不低于原峰的次数 + 1) / (200 + 1)
    n_ge = int(np.sum(perm_max >= max_power))
    tail_prob = (n_ge + 1) / (N_PERM + 1)

    data = np.loadtxt(tmp / "input.csv", delimiter=",", skiprows=1, ndmin=2)
    t, y = data[:, 0], data[:, 1]
    phase = ((t - t.min()) / best_period) % 1.0

    periods = 1.0 / freq[::-1]          # 升序周期,便于绘图
    power = orig_power[::-1]
    return {
        "best_period": best_period,
        "max_power": max_power,
        "tail_prob": tail_prob,
        "n_perm_ge": n_ge,
        "n_perm": N_PERM,
        "n_freq": N_FREQ,
        "power_png": make_power_plot(periods, power, best_period,
                                     max_power, job.period_min,
                                     job.period_max),
        "phase_png": make_phase_plot(phase, y, best_period),
    }


# --------------------------------------------------------------------------- #
# 批处理执行与收尾
# --------------------------------------------------------------------------- #

def _safe_terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        if proc.returncode is None:
            proc.terminate()
    except ProcessLookupError:
        pass


def _safe_kill(proc: asyncio.subprocess.Process) -> None:
    try:
        if proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass


async def _wait_all(job: Job) -> list[int]:
    """等待本批全部子进程退出;任一失败(非取消)则终止其余兄弟进程。"""
    tasks = {asyncio.ensure_future(p.wait()): p for p in job.procs}
    returncodes: list[int] = []
    pending = set(tasks)
    while pending:
        done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            rc = task.result()
            returncodes.append(rc)
            if rc != 0 and not job.cancel_requested:
                for sibling in job.procs:
                    _safe_terminate(sibling)
    return returncodes


async def run_job(job: Job) -> None:
    """拉起两个计算子进程,等待退出,清理临时文件,最后才公布结局。"""
    tmp = job.tmpdir
    try:
        base_seed = secrets.randbelow(2**31 - 1)
        for k in range(WORKERS_PER_JOB):
            err_path = tmp / f"err_{k}.log"
            cmd = [
                # 用当前解释器拉起计算子进程
                *(_python_cmd()),
                str(WORKER_PY),
                "--input", str(tmp / "input.csv"),
                "--output", str(tmp / f"out_{k}.json"),
                "--fmin", repr(1.0 / job.period_max),
                "--fmax", repr(1.0 / job.period_min),
                "--nfreq", str(N_FREQ),
                "--nperm", str(N_PERM // WORKERS_PER_JOB),
                "--seed", str(base_seed + k),
            ]
            if k == 0:
                cmd.append("--compute-original")
            err_fh = open(err_path, "wb")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=err_fh)
            finally:
                err_fh.close()  # 子进程已继承副本,父进程可立即关闭
            job.procs.append(proc)

        returncodes = await _wait_all(job)

        # 此时本批全部计算子进程均已退出;先在本地算出结局
        state, result, error = "failed", None, None
        try:
            if job.cancel_requested:
                state = "cancelled"
            elif any(rc != 0 for rc in returncodes):
                logs = []
                for k in range(WORKERS_PER_JOB):
                    err_path = tmp / f"err_{k}.log"
                    if err_path.exists():
                        text = err_path.read_text(
                            encoding="utf-8", errors="replace").strip()
                        if text:
                            logs.append(f"子进程 {k}:{text[-800:]}")
                error = "计算子进程异常退出。" + ("\n" + "\n".join(logs) if logs else "")
            else:
                result = combine_results(job)
                state = "done"
        except Exception as exc:  # 汇总阶段出错也视为失败
            state, result, error = "failed", None, f"结果汇总失败:{exc}"

        # 临时文件清完之前,不公布结局、不释放名额
        shutil.rmtree(tmp, ignore_errors=True)

        # 公布结局(若已受理取消,只会是 cancelled,不会发布迟到结果)
        job.result = result
        job.error = error
        job.state = state
        job.finished_at = time.time()
        # state 离开 running 即释放名额,另一批不受影响
    except Exception as exc:  # 拉起子进程失败等兜底
        # 已拉起的子进程必须先终止并回收,再清理、公布、释放名额
        for proc in job.procs:
            _safe_terminate(proc)
        if job.procs:
            await asyncio.gather(
                *(p.wait() for p in job.procs), return_exceptions=True)
        shutil.rmtree(tmp, ignore_errors=True)
        job.error = f"内部错误:{exc}"
        job.state = "failed"
        job.finished_at = time.time()


def _python_cmd() -> list[str]:
    import sys
    return [sys.executable, "-I"]


def _evict_old_jobs() -> None:
    """内存态记录设上限,优先淘汰最早的已终结任务。"""
    if len(JOBS) <= MAX_KEPT_JOBS:
        return
    terminal = sorted((j for j in JOBS.values() if j.state in TERMINAL_STATES),
                      key=lambda j: j.created_at)
    for j in terminal[: len(JOBS) - MAX_KEPT_JOBS]:
        JOBS.pop(j.id, None)


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #

app = FastAPI(title="周期变星候选筛选", version="1.0.0")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@app.get("/api/sample.csv")
async def sample_csv() -> FileResponse:
    if not SAMPLE_CSV.exists():
        raise HTTPException(404, "样例文件不存在")
    return FileResponse(SAMPLE_CSV, media_type="text/csv",
                        filename="sample_star.csv")


@app.post("/api/jobs", status_code=201)
async def create_job(file: UploadFile = File(...),
                     period_min: float = Form(...),
                     period_max: float = Form(...)) -> dict:
    validate_period_range(period_min, period_max)

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "文件过大")
    t, y = validate_csv(raw)

    # 最多两批并行,超额拒绝(单事件循环内检查与登记之间无 await,无竞态)
    running = sum(1 for j in JOBS.values() if j.state == "running")
    if running >= MAX_PARALLEL_JOBS:
        raise HTTPException(429, "已有两批正在计算,请稍后再试或取消其中一批")

    job = Job(period_min=period_min, period_max=period_max,
              n_rows=len(t), filename=file.filename or "upload.csv")
    # 子进程输入:规范化后的两列 CSV(临时文件,收尾时清除)
    np.savetxt(job.tmpdir / "input.csv", np.column_stack([t, y]),
               delimiter=",", header="time_day,flux", comments="",
               fmt="%.10g")
    JOBS[job.id] = job
    _evict_old_jobs()
    asyncio.ensure_future(run_job(job))
    return {"job_id": job.id, "state": job.state}


@app.get("/api/jobs")
async def list_jobs() -> dict:
    running = sum(1 for j in JOBS.values() if j.state == "running")
    jobs = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
    return {"running": running, "max_parallel": MAX_PARALLEL_JOBS,
            "jobs": [j.summary() for j in jobs]}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.summary()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.state in TERMINAL_STATES:
        # 重复取消:不重复释放名额,只回报现状
        return {"job_id": job.id, "state": job.state,
                "detail": "该批已终结,取消不再生效"}
    if not job.cancel_requested:
        job.cancel_requested = True
        for proc in job.procs:
            _safe_terminate(proc)
        # 兜底:5 秒后仍未退出则强杀,保证名额最终一定释放
        asyncio.get_running_loop().call_later(
            5.0, lambda j=job: [_safe_kill(p) for p in j.procs])
    return {"job_id": job.id, "state": "cancelling",
            "detail": "取消已受理,等待子进程退出并清理临时文件"}


@app.get("/api/jobs/{job_id}/plot/{kind}.png")
async def job_plot(job_id: str, kind: str) -> Response:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.state != "done" or not job.result:
        raise HTTPException(409, "结果尚未公布")
    key = {"power": "power_png", "phase": "phase_png"}.get(kind)
    if key is None:
        raise HTTPException(404, "未知图类型")
    return Response(content=job.result[key], media_type="image/png")
