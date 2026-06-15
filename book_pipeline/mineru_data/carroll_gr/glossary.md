# Carroll《Spacetime and Geometry: An Introduction to General Relativity》翻譯規範

繁體中文（zh-TW，台灣物理慣用語）。本檔由 `/translate-book` skill 派出的 sub-agent 必讀。

## 鐵則（絕對保留原樣）

- LaTeX：`$...$`、`$$...$$`、`\(...\)`、`\[...\]` 整段不動，**包括 `\text{}` / `\mbox{}` / `\mathrm{}` 內的英文**
- 引用編號：`Eq. (1.1)`、`Fig. 1.2`、`Chapter 3`、`Section 2.4`、`Problem 5.7`、`Example 4.2`、`Appendix B`、`Table 1.1` — 保留英文 + 編號
- 人名：保留英文（Einstein、Lorentz、Minkowski、Riemann、Ricci、Christoffel、Schwarzschild、Kerr、Reissner、Nordström、Hawking、Penrose、Friedmann、Lemaître、Robertson、Walker、de Sitter、Killing、Newton、Maxwell、Gauss、Bianchi、Jacobi、Lie、Stokes、Cartan、Weyl、Levi-Civita、Poincaré、Planck、Dirac、Feynman、Wheeler、Misner、Thorne、Carroll、Birkhoff、Komar、Noether、Lorenz、Fermi、Walker…）
- 定理 / 定律 / 方程第一次出現：原文 + 中譯（例 `Bianchi identity → Bianchi 恆等式`）；後續直接中譯
- 數字、單位（`c`, `G`, `\hbar`, `M_\odot`, `\text{km}`, `\text{eV}`）保留原樣
- 程式碼 / 變數名 / 指標（`g_{\mu\nu}`, `R^\rho_{\sigma\mu\nu}`, `\Gamma^\lambda_{\mu\nu}`, `\nabla_\mu`, `\partial_\mu`, `T^{\mu\nu}`, `\eta_{\mu\nu}`）保留原樣
- 抽象指標標記（abstract index notation）保留原樣
- 圖表 axis label 內嵌 LaTeX 中，不動

## 譯名表

### 基礎時空與相對論

| en | zh-TW |
|---|---|
| spacetime | 時空 |
| special relativity | 狹義相對論 |
| general relativity | 廣義相對論 |
| inertial frame | 慣性系 |
| reference frame | 參考系 |
| Lorentz transformation | Lorentz 變換 |
| Lorentz boost | Lorentz 推進 |
| Poincaré group | Poincaré 群 |
| Minkowski space / spacetime | Minkowski 空間 / 時空 |
| Minkowski metric | Minkowski 度規 |
| event | 事件 |
| worldline | 世界線 |
| worldsheet | 世界面 |
| light cone | 光錐 |
| null cone | 零錐 |
| causal structure | 因果結構 |
| timelike / spacelike / null | 類時 / 類空 / 類光（零） |
| proper time | 固有時 |
| proper length | 固有長度 |
| proper distance | 固有距離 |
| invariant interval | 不變間隔 |
| line element | 線元 |
| time dilation | 時間膨脹 |
| length contraction | 長度收縮 |
| relativity of simultaneity | 同時性的相對性 |
| four-vector | 四向量 |
| four-velocity | 四速度 |
| four-momentum | 四動量 |
| four-acceleration | 四加速度 |
| rest mass | 靜質量 |
| rest energy | 靜能 |
| equivalence principle | 等效原理 |
| weak equivalence principle | 弱等效原理 |
| Einstein equivalence principle | Einstein 等效原理 |
| strong equivalence principle | 強等效原理 |
| inertial mass / gravitational mass | 慣性質量 / 重力質量 |

### 流形與微分幾何

| en | zh-TW |
|---|---|
| manifold | 流形 |
| differentiable manifold | 可微流形 |
| chart | 座標卡 |
| atlas | 圖集 |
| coordinate system | 座標系 |
| coordinate basis | 座標基底 |
| non-coordinate basis | 非座標基底 |
| tangent space | 切空間 |
| cotangent space | 餘切空間 |
| tangent vector | 切向量 |
| cotangent vector | 餘切向量 |
| dual vector / one-form | 對偶向量 / 單形 |
| vector field | 向量場 |
| tensor | 張量 |
| tensor field | 張量場 |
| tensor product | 張量積 |
| rank | 秩 |
| contraction | 縮並 |
| index | 指標 |
| upper / lower index | 上 / 下指標 |
| covariant / contravariant | 協變 / 逆變 |
| raising / lowering indices | 升 / 降指標 |
| symmetric / antisymmetric | 對稱 / 反對稱 |
| symmetrization / antisymmetrization | 對稱化 / 反對稱化 |
| Kronecker delta | Kronecker δ |
| Levi-Civita symbol | Levi-Civita 符號 |
| Levi-Civita tensor | Levi-Civita 張量 |
| differential form | 微分形式 |
| p-form | p-形式 |
| exterior derivative | 外微分 |
| exterior product / wedge product | 外積 / 楔積 |
| pullback | 拉回 |
| pushforward | 推前 |
| Lie derivative | Lie 導數 |
| Lie bracket | Lie 括號 |
| Lie group / Lie algebra | Lie 群 / Lie 代數 |
| diffeomorphism | 微分同胚 |
| isometry | 等度規 / 等距同構 |
| Killing vector | Killing 向量 |
| Killing equation | Killing 方程 |
| integral curve | 積分曲線 |
| flow | 流 |
| orientation | 定向 |

### 度規、聯絡、曲率

| en | zh-TW |
|---|---|
| metric | 度規 |
| metric tensor | 度規張量 |
| signature | 符號差 |
| Lorentzian metric | Lorentz 度規 |
| Riemannian metric | Riemann 度規 |
| inverse metric | 逆度規 |
| determinant of the metric | 度規行列式 |
| volume element | 體積元 |
| connection | 聯絡 |
| affine connection | 仿射聯絡 |
| metric-compatible | 與度規相容 |
| torsion-free | 無撓 |
| Levi-Civita connection | Levi-Civita 聯絡 |
| Christoffel symbol | Christoffel 符號 |
| covariant derivative | 協變導數 |
| parallel transport | 平行移動 |
| geodesic | 測地線 |
| geodesic equation | 測地線方程 |
| affine parameter | 仿射參數 |
| null geodesic | 零測地線 |
| timelike geodesic | 類時測地線 |
| geodesic deviation | 測地線偏離 |
| curvature | 曲率 |
| Riemann curvature tensor | Riemann 曲率張量 |
| Ricci tensor | Ricci 張量 |
| Ricci scalar | Ricci 純量 |
| scalar curvature | 純量曲率 |
| sectional curvature | 截面曲率 |
| Weyl tensor | Weyl 張量 |
| Einstein tensor | Einstein 張量 |
| Bianchi identity | Bianchi 恆等式 |
| tetrad / vielbein | 標架 / vielbein |
| spin connection | 自旋聯絡 |
| frame field | 標架場 |

### Einstein 方程與引力

| en | zh-TW |
|---|---|
| Einstein equation | Einstein 方程 |
| Einstein field equation | Einstein 場方程 |
| cosmological constant | 宇宙常數 |
| stress-energy tensor | 應力-能量張量 |
| energy-momentum tensor | 能量-動量張量 |
| perfect fluid | 完美流體 |
| dust | 塵 |
| pressure | 壓力 |
| energy density | 能量密度 |
| equation of state | 物態方程 |
| weak / strong / dominant energy condition | 弱 / 強 / 主能量條件 |
| null energy condition | 零能量條件 |
| Newtonian limit | Newton 極限 |
| weak field | 弱場 |
| linearized gravity | 線性化重力 |
| gravitational wave | 重力波 |
| transverse-traceless gauge | 橫向無跡規範 |
| TT gauge | TT 規範 |
| gauge | 規範 |
| gauge transformation | 規範變換 |
| harmonic gauge | 諧和規範 |
| Lorenz gauge | Lorenz 規範 |
| Einstein-Hilbert action | Einstein-Hilbert 作用量 |
| action | 作用量 |
| Lagrangian | Lagrange 量 |
| variational principle | 變分原理 |
| Palatini formulation | Palatini 形式 |

### 黑洞解

| en | zh-TW |
|---|---|
| Schwarzschild solution | Schwarzschild 解 |
| Schwarzschild metric | Schwarzschild 度規 |
| Schwarzschild radius | Schwarzschild 半徑 |
| event horizon | 事件視界 |
| horizon | 視界 |
| Killing horizon | Killing 視界 |
| apparent horizon | 表觀視界 |
| singularity | 奇點 |
| coordinate singularity | 座標奇點 |
| curvature singularity | 曲率奇點 |
| Birkhoff's theorem | Birkhoff 定理 |
| black hole | 黑洞 |
| white hole | 白洞 |
| Kruskal extension | Kruskal 延拓 |
| Kruskal–Szekeres coordinates | Kruskal–Szekeres 座標 |
| Eddington–Finkelstein coordinates | Eddington–Finkelstein 座標 |
| Penrose diagram | Penrose 圖 |
| conformal diagram | 共形圖 |
| conformal infinity | 共形無窮遠 |
| Reissner–Nordström solution | Reissner–Nordström 解 |
| Kerr solution | Kerr 解 |
| Kerr–Newman solution | Kerr–Newman 解 |
| ergosphere | 能層 |
| Penrose process | Penrose 過程 |
| surface gravity | 表面重力 |
| extremal black hole | 極端黑洞 |
| no-hair theorem | 無毛定理 |
| Hawking radiation | Hawking 輻射 |
| black hole thermodynamics | 黑洞熱力學 |
| entropy | 熵 |
| temperature | 溫度 |

### 宇宙學

| en | zh-TW |
|---|---|
| cosmology | 宇宙學 |
| cosmological principle | 宇宙學原理 |
| homogeneity / isotropy | 均勻性 / 各向同性 |
| FRW / FLRW metric | FRW / FLRW 度規 |
| Friedmann equation | Friedmann 方程 |
| scale factor | 尺度因子 |
| Hubble parameter | Hubble 參數 |
| Hubble constant | Hubble 常數 |
| Hubble's law | Hubble 定律 |
| redshift | 紅移 |
| cosmological redshift | 宇宙學紅移 |
| comoving coordinates | 共動座標 |
| comoving distance | 共動距離 |
| proper distance | 固有距離 |
| critical density | 臨界密度 |
| density parameter | 密度參數 |
| dark matter | 暗物質 |
| dark energy | 暗能量 |
| inflation | 暴脹 |
| de Sitter space | de Sitter 空間 |
| anti–de Sitter space | 反 de Sitter 空間 |
| big bang | 大霹靂 |
| early universe | 早期宇宙 |
| nucleosynthesis | 核合成 |
| recombination | 復合 |
| CMB / cosmic microwave background | 宇宙微波背景 |
| open / closed / flat universe | 開放 / 封閉 / 平坦宇宙 |

### 因果結構與整體幾何

| en | zh-TW |
|---|---|
| Cauchy surface | Cauchy 面 |
| Cauchy problem | Cauchy 問題 |
| globally hyperbolic | 整體雙曲 |
| chronological future / past | 時序未來 / 過去 |
| causal future / past | 因果未來 / 過去 |
| achronal | 非時序 |
| trapped surface | 陷俘面 |
| singularity theorem | 奇點定理 |
| geodesic incompleteness | 測地線不完備 |
| cosmic censorship | 宇宙監督 |
| naked singularity | 裸奇點 |
| asymptotically flat | 漸近平坦 |
| ADM mass | ADM 質量 |
| Komar integral | Komar 積分 |

### 通用詞

| en | zh-TW |
|---|---|
| problem | 習題 |
| example | 例題 |
| solution | 解答 |
| exercise | 練習 |
| proof | 證明 |
| derivation | 推導 |
| derive | 推導 |
| show that | 證明 |
| prove | 證明 |
| find | 求出 |
| calculate | 計算 |
| compute | 計算 |
| determine | 求出 / 確定 |
| consider | 考慮 |
| sketch | 繪出 / 草繪 |
| verify | 驗證 |
| check | 檢驗 |
| symmetry | 對稱性 |
| approximation | 近似 |
| limit | 極限 |
| boundary | 邊界 |
| surface | 表面 / 曲面 |
| volume | 體積 |
| sphere / spherical | 球 / 球形 |
| cylinder / cylindrical | 圓柱 / 圓柱形 |
| infinitesimal | 無窮小 |
| uniform | 均勻 |
| static | 靜態 |
| stationary | 穩態 / 靜止的 |
| spherically symmetric | 球對稱 |
| axisymmetric | 軸對稱 |
| asymptotic | 漸近 |
| local / global | 局部 / 整體 |
| coordinate-free | 不依賴座標 |
| observer | 觀察者 |
| test particle | 試驗粒子 |
| free fall | 自由下落 |
| locally inertial | 局部慣性 |

## 標點與語氣

- 句末英文標點 `. , ; :` **保留**（不要改全形）
- 中文句子**內部與句末**用全形（。，；：）
- 不加譯註、不擴寫、不刪節
- 不要 markdown fence、不要說明文字
- **不要輸出任何 0x00–0x1f 控制字元**
