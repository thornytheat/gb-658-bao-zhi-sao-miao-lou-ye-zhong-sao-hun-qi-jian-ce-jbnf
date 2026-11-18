# 扫描期版质检台

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18217。测试：docker compose exec -T app pytest -q

## 模块

- M1 清单采信：页面 /intake，接口 /api/scans/import
- M2 期版契约：页面 /issues，接口 /api/issues/normalize
- M3 页码序列：页面 /pages，接口 /api/pages/check
- M4 重扫判定：页面 /rescans，接口 /api/rescans/evaluate
- M5 混期归因：页面 /mixed，接口 /api/mixed/evaluate
- M6 批次对账：页面 /batches，接口 /api/reconcile
- M7 缺页清单：页面 /missing，接口 /api/missing/list、/api/missing/reassign

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

## 缺页清单出单口径

- 页归属已定（页眉一致或补证改期）才进入计页；页眉空或不可辨列“待裁决 WAIT”，不补扫、不计覆盖。
- 待裁决候选同时在原期与页眉指向的期出现，印务不会在一期里误判“无候选”而重复补扫。
- 只有确定无候选的页才列“必须补扫 RESCAN”。混期裁决结果直接决定选片，选片结果再用于核缺页。
- 补证改期在一次事务里重算原期与新期两份缺页；中断回滚保留旧版清单，重试不重复记改期。

