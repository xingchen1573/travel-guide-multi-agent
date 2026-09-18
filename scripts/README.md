# 辅助脚本

根目录只保留长期使用的正式入口：`run_mvp.py`、`run_feishu_worker.py`、`run_review_mvp.py` 和 `run_scenario_eval.py`。其他命令按用途放在本目录，并统一从项目根目录使用 `python -m` 运行。

## 维护与回填

```bash
python -m scripts.maintenance.backfill_evidence_checks --help
python -m scripts.maintenance.backfill_reference_usages --help
```

## 呈现与交付

```bash
python -m scripts.delivery.run_annotate <project_id> <topic>
python -m scripts.delivery.run_generate_site <project_id>
python -m scripts.delivery.run_generate_poster <project_id>
```

## 素材

```bash
python -m scripts.materials.run_material_agent <目的地> <需求描述>
python -m scripts.materials.run_submit_material --help
python -m scripts.materials.run_submit_material_batch --help
```

## 专项验证

```bash
python -m scripts.e2e.run_langgraph_fixed_route_e2e --help
python -m scripts.e2e.run_route_feasibility_smoke --help
```

专项验证可能调用真实模型、Web、高德或其他收费服务，执行前先阅读对应脚本说明和相关设计文档。

## 已移除的一次性脚本

`run_gap_supplement_graph_e2e.py`、`run_pitfall_agent_e2e.py`、`run_poi_detail_e2e.py` 和 `run_qinggan_linear_8d.py` 只记录早期固定样本验证，当前已由 `run_scenario_eval.py`、固定路线 Skill 和测试套件覆盖，不再保留可执行副本；历史结论仍在文档归档和 Git 历史中可追溯。
