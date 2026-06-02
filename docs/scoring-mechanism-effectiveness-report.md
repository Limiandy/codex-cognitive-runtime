# Scoring Mechanism Effectiveness Report

Generated: 2026-06-02 18:38:09 CST

## Objective

Prove that the seed skill scoring mechanism is observable and affects runtime selection decisions.

This report focuses on whether `seed_skill_selection_scores` changes the selected seed skills according to task semantics, instead of relying on hidden heuristics or broad industry blacklists.

## Test Commands

```bash
PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest tests.test_runtime_skill tests.test_runtime_trace tests.test_runtime_observer tests.test_hooks
```

Result:

```text
Ran 98 tests in 6.877s
OK
```

```bash
PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest discover -s tests
```

Result:

```text
Ran 265 tests in 578.574s
OK
```

## Scoring Evidence Source

The proof script executed `MemoryService.prompt_context(...)` for each scenario and read scoring evidence from trace event:

```text
basis_retrieved.metadata_json.seed_skill_selection_scores
```

Each score record includes:

```text
id
name
score
rank
selected
target_surfaces
target_domains
```

## Scenario Results

### Brand Logo Task

Prompt:

```text
帮我画一个品牌 logo
```

Task understanding:

```text
skill_needed: true
domain: brand_design
task_type: brand_logo_design
surfaces: design
role_primary: 品牌设计专家
```

Top selected seed skills:

| Rank | Skill | Score | Selected | Target |
| --- | --- | ---: | --- | --- |
| 1 | Brand Guardian | 9 | true | design / brand |
| 2 | UI Designer | 9 | true | design / brand |
| 3 | Visual Storyteller | 9 | true | design / brand |
| 4 | Whimsy Injector | 9 | true | design / brand |

Conclusion:

The scoring mechanism correctly promotes brand/design seed skills, including Brand Guardian.

### WeChat Mini Program UI Task

Prompt:

```text
优化微信小程序订单页 UI 布局
```

Task understanding:

```text
skill_needed: true
domain: software_engineering
task_type: frontend_ui_redesign
surfaces: frontend, ui, ux
role_primary: 前端工程专家
```

Top selected seed skills:

| Rank | Skill | Score | Selected | Target |
| --- | --- | ---: | --- | --- |
| 1 | WeChat Mini Program Developer | 13 | true | frontend/ui/ux / wechat |
| 2 | UI Designer | 4 | true | frontend/ui/ux / wechat |
| 3 | Mobile App Builder | 3 | true | frontend/ui/ux / wechat |
| 4 | UX Architect | 3 | true | frontend/ui/ux / wechat |

Conclusion:

The scoring mechanism does not exclude mini-program skills merely because the task is UI. When the prompt explicitly targets WeChat mini programs, WeChat Mini Program Developer is ranked first.

### Generic Frontend UI Task

Prompt:

```text
调整重置按钮大小，增加下拉 select placeholder
```

Task understanding:

```text
skill_needed: true
domain: software_engineering
task_type: frontend_ui_redesign
surfaces: frontend, ui, ux
role_primary: 前端工程专家
```

Top selected seed skills:

| Rank | Skill | Score | Selected | Target |
| --- | --- | ---: | --- | --- |
| 1 | UI Designer | 4 | true | frontend/ui/ux |
| 2 | Mobile App Builder | 4 | true | frontend/ui/ux |
| 3 | UX Architect | 4 | true | frontend/ui/ux |
| 4 | UX Researcher | 4 | true | frontend/ui/ux |

Conclusion:

The scoring mechanism avoids over-selecting WeChat, Feishu, Roblox, XR, or marketing-specific skills when the prompt only expresses a generic frontend UI need.

### Memory Statement

Prompt:

```text
[trace-rerun-0098/project_exp] 经验：工程任务必须先 inspect，再最小修改，最后跑 unittest。
```

Task understanding:

```text
skill_needed: false
domain: general
selected_seed_skill_ids: []
context_has_task_rules: false
```

Conclusion:

The scoring mechanism is not invoked for non-execution memory statements. This prevents memory capture statements from starting runtime workflows or selecting seed skills.

## Overall Conclusion

The scoring mechanism is effective in the tested runtime paths because:

1. Score records are written to trace metadata and can be inspected after execution.
2. Brand tasks promote brand/design skills.
3. WeChat mini-program UI tasks promote WeChat Mini Program Developer without treating mini-program UI as invalid.
4. Generic frontend UI tasks promote generic UI/UX skills instead of unrelated domain-specific skills.
5. Non-action memory statements do not invoke scoring or runtime skill injection.

The mechanism is now observable enough to debug future failures by checking whether the issue is in task understanding, candidate recall, compatibility scoring, or fragment distillation.
