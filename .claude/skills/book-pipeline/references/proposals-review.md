# Proposals — 通用建議系統（agent 提案 → owner 採納）

pipeline 各階段 agent 跑到一半常發現「值得跨書泛化、但 autonomous 不該擅改核心碼」的改進點。
本系統把這類建議收斂成**通用、並行安全、schema 強制**的佇列：**任何 agent 一行 CLI 提案，owner
集中 review→決策→回歸閘→記狀態**。不再手寫 markdown（易漂移、單檔多寫者互踩）。

## 架構

- **儲存（source of truth）**：`book_pipeline/proposals.d/<id>.json`，一案一檔。各 agent 寫自己的
  id 檔（`os.O_EXCL` 認領 id）→ 多 agent 並行零爭用；`jsonio.atomic_write_json` 保證不留半截。
- **人類視圖**：`book_pipeline/proposals.d/_index.md`，由 `render` 從 JSON 生成（**勿手改**；lint 會比對同步）。
- **CLI**：`uv run python -m book_pipeline.proposals <verb>`（`book_pipeline/proposals.py`）。
- **id**：`P-YYYY-MM-DD-<slug>`（同 slug 並行碰撞自動 `-2`/`-3`…）。

## 生命週期

```
agent: proposals propose …            # 任何階段隨手提案（狀態 proposed）
owner: proposals list --status proposed   # 盤點待採納
owner: proposals check                # [math] 比對 live 殘餘，揪出 already-resolved
owner: 依決策樹定 macro/normalize/override/reject
owner: （採納碼層改動後）proposals gate    # [math] 回歸閘：backfill + 殘餘不得上升
owner: proposals resolve <id> --status … --resolution …   # 記決策（自動重生 _index.md）
owner: commit（含 store 變動 + 碼變動；daemon 不 commit）
```

## CLI

| verb | 用途 |
|---|---|
| `propose --domain D --type T --title … [--detect … --evidence … --proposal … --risk … --source …]` | 提案，印 id |
| `list [--domain D] [--status S]` | 列表 |
| `show <id>` | 單案 JSON |
| `resolve <id> --status accepted\|rejected\|superseded --resolution … [--disposition …]` | 記決策 |
| `lint` | schema 驗證 + _index 同步檢查（CI/測試用） |
| `render` | 由 store 重生 _index.md |
| `check` | **[math hook]** 對 proposed 數 live 殘餘 occ（0 → already-resolved 候選） |
| `gate [slug…]` | **[math hook]** backfill 全 corpus 重 parse+重驗，任一書殘餘上升即非零退出 |

## 受控詞彙

- `status`：`proposed | accepted | rejected | superseded`。非 proposed 必附 `resolution`。
- `type`（依 domain，見 `DOMAINS`）：math = `macro | normalize-rule | override`；catalog = `override | rule`。
- reject 理由代碼：`pseudo-macro-guard | already-resolved | semantically-ambiguous | single-book | unsafe | superseded | out-of-scope`。

## 決策樹（採納時對每條 proposed 問）

1. **真符號缺定義**（單一無歧義展開、跨多書）→ `accepted` 升 **Layer 0 macro**（`math_macros.json`）。
2. **機械 OCR pattern**（確定性字串轉換、語意保持）→ `accepted` 升 **Layer 1 normalize 規則**（`math_normalize.py`）。
3. **OCR 黏字偽巨集**（`\Nu`/`\muA`/`\cdotE`…，token 歧義或上下文相依）→ `rejected(pseudo-macro-guard
   [+ semantically-ambiguous])`，改 **per-slug override**；偽巨集另登入 `test_no_ocr_glue_pseudomacros` 禁收清單。
4. **已被 override 清掉 / 單書 / 不可逆亂碼** → `rejected(already-resolved | single-book)` 或 §8 accept。

> 反例教訓（本系統首航）：autonomous 曾提 `\Nu→\nu` macro。實測所有語境皆大寫 N（高斯映射 N、
> Rudin 自然數界 N、Dummit 範數 N_{K/F}），`\nu`（小寫）全錯 → 驗證決策樹第 3 條：偽巨集走 override，
> 不升 macro。`proposals check` 也即時抓出 `\muA` live 0 occ（已被 override 清零）→ already-resolved。

## 升級 Layer 0/1 的強制閘（accepted macro/normalize 必過，缺一不可）

1. **before/after fixture**（真實壞樣本）寫進 `test_math_macros.py` / `test_math_normalize.py`。
2. **冪等** `f(f(x))==f(x)`；對正確式 **no-op**（回歸樣本）。
3. **全 corpus 回歸** `proposals gate`（= `backfill_math` 重 parse+重套+重驗）：**任一書殘餘上升即回退**。
4. macros：對照 `test_no_ocr_glue_pseudomacros` 偽巨集邊界；改 `math_macros.json` **必跑** `build.gen_macros`（同步 reader 內聯）。
5. resolve 記狀態 + commit（owner 親自，daemon 不 commit）。

## 新增 domain（擴充指南）

`proposals.py` 的 `DOMAINS = {domain: {types, checker}}`：加一筆即開新 domain。
- `types`：該 domain 合法 `--type` 集合（lint 據此驗）。
- `checker`：`"math"` 走內建 live-occ 比對；`None` 表暫無自動 triage（owner 手判）。
- 若該 domain 要 `gate`，比照 `cmd_gate` 寫對應回歸（讀該 domain 的殘餘度量）。

範例（catalog audit agent 提「跨書 catalog 規則」）：
`proposals propose --domain catalog --type rule --source catalog_audit --title '…' --evidence '…' --proposal '…'`
