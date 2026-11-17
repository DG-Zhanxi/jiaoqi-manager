# 数据来源、许可与处理

本仓库原创代码采用MIT许可证；本文件列出的第三方数据与原始研究文件采用其各自的CC BY 4.0许可。

| 数据 | 作者、机构与版本 | 链接 | 仓库位置 |
|---|---|---|---|
| 柔性车间实例 | Lele Li，合肥工业大学，2023，V3 | https://doi.org/10.17632/mwrgpx7hhp.3 | `data/raw/open_alternatives/hfut` |
| 白车身焊装任务 | Qi Xia，长春工业大学，2026，V1 | https://doi.org/10.17632/gswbxhjmnw.1 | `data/raw/automotive/ALGORITHMS.py`、`data/processed/automotive_biw.json` |
| 柔性包装生产 | Masmur Tarigan，Universitas INDONUSA Esa Unggul，2025，V1 | https://doi.org/10.17632/h66hb89k6z.1 | `data/raw/open_alternatives/packaging` |

许可全文：https://creativecommons.org/licenses/by/4.0/

合工大原始工作簿保留原样，由`app/backend/adapters/hfut.py`转换工序、批量与时间字段。包装工作簿保留原样，工艺及加工时长由研究适配程序计算。焊装JSON由原始研究文件`ALGORITHMS.py`中的工业案例提取为43任务、8类资源及协作预算结构，JSON中保留原文件SHA256。

本项目统一使用秒作为时间单位，并在实验协议中设置交期和停机事件；处理后的数据与本项目运行结果随同发布。仓库未修改原始工作簿和原始研究文件。原始文件中的研究算法按CC BY 4.0保留，项目调度实现位于`app/`。
