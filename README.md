# 周期变星候选筛选(Lomb–Scargle + 置换检验)

面向天文巡测数据员的网页工具:上传单颗星的测光序列,自动搜索周期信号,
输出最高峰周期、功率谱、相位折叠散点与置换尾概率,供人工复核。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

页面上可直接点击「载入虚构样例并提交」(`sample/sample_star.csv`,
一颗周期约 4.25 天的虚构脉动变星,160 个观测点)。

无 Docker 时本地运行:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 输入要求

- CSV 含 `time_day`、`flux` 两列(允许其他列存在,只取这两列);
- 至少 10 行;两列均为有限数值(无空值/NaN/Inf);观测时刻互异;
- 周期范围须满足 `0 < 下限 < 上限`。

## 计算流程

1. 周期范围取倒数得到频率范围 `[1/上限, 1/下限]`,均匀取 **10000** 个频率点;
2. 用**带浮动均值**的 Lomb–Scargle(`astropy.timeseries.LombScargle`,
   `fit_mean=True`)计算功率谱,取最高峰周期;
3. **置换检验**:固定时刻,随机重排光通量 **200** 次,每次重算全频段
   最大功率;尾概率 = `(不低于原峰的次数 + 1) / 201`;
4. 展示最高峰周期、峰值功率、尾概率、功率谱图与按峰周期折叠的相位散点图。

## 并发与生命周期语义

- 每批任务使用 **2 个计算子进程**(各承担 100 次置换,worker 0 另算原始
  功率谱);最多 **2 批并行**,超额提交返回 **HTTP 429**;
- 任务可查询、可取消(`POST /api/jobs/{id}/cancel`);
- 无论成功、失败还是取消,都要等本批全部子进程退出、临时文件清理完毕,
  才公布结局并释放名额;另一批不受影响;
- 取消一经受理即终止子进程,**不会发布迟到结果**;重复取消幂等,不会
  重复释放名额;
- 全部任务状态只存内存(重启即清空);计算期临时文件随收尾删除。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 网页 |
| POST | `/api/jobs` | 提交批处理(multipart:`file`、`period_min`、`period_max`)→ 201 / 400 / 429 |
| GET | `/api/jobs` | 批处理列表与名额占用 |
| GET | `/api/jobs/{id}` | 单批详情(含指标) |
| POST | `/api/jobs/{id}/cancel` | 取消(幂等) |
| GET | `/api/jobs/{id}/plot/power.png` | 功率谱图(成功后) |
| GET | `/api/jobs/{id}/plot/phase.png` | 相位折叠散点图(成功后) |
| GET | `/api/sample.csv` | 虚构样例数据 |

示例:

```bash
curl -F "file=@sample/sample_star.csv" -F "period_min=2" -F "period_max=10" \
     http://localhost:8000/api/jobs
```

## 目录结构

```
app/main.py            FastAPI 主程序:校验、任务队列、子进程编排、收尾与出图
app/worker.py          计算子进程:Lomb–Scargle 功率谱 + 置换检验
app/static/index.html  网页(原生 JS,无外部依赖)
sample/make_sample.py  虚构样例生成脚本(固定种子,可复现)
sample/sample_star.csv 已生成的样例数据(160 行,真周期 4.25 天)
Dockerfile / docker-compose.yml
```

重新生成样例:`python sample/make_sample.py`
