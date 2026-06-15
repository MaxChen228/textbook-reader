# Feynman《Lectures on Physics, Vol. III: Quantum Mechanics》翻譯規範

繁體中文（zh-TW，台灣物理慣用語）。本檔由 `/translate-book` skill 派出的 sub-agent 必讀。

## 鐵則（絕對保留原樣）

- LaTeX：`$...$`、`$$...$$`、`\(...\)`、`\[...\]` 整段不動，**包括 `\text{}` / `\mbox{}` / `\mathrm{}` 內的英文**
- 引用編號：`Eq. (1.1)`、`Fig. 1.2`、`Section 2.3`、`Chapter 1`、`Problem 1.5` — 保留英文 + 編號
- 人名：保留英文（Heisenberg、Schrödinger、Dirac、Stern、Gerlach、Bohr、Einstein、Pauli、Wigner、Ehrenfest、Feynman、Born、Yukawa、Rutherford、Coulomb、Bessel、Hermite、Laguerre、Legendre、Lippmann、Schwinger、Klein、Gordon、Weyl、Majorana、Lorentz、Fermi、Bose、Slater、Hartree、Fock、Heitler、London、Aharonov、Bohm、Brillouin、Rayleigh…）
- 理論/實驗第一次出現：原文 + 中譯（例 `Stern–Gerlach experiment → Stern–Gerlach 實驗`）；後續直接中譯
- 數字、單位（`10 T/m`, `1000°C`, `\hbar`）保留原樣
- 程式碼 / 變數名（`s_z`, `|+>`, `H_0`, `J^2`, `|j,m>`, `D(R)`）保留原樣

## 譯名表

### 通用

| en | zh-TW |
|---|---|
| spin | 自旋 |
| eigenstate / eigenvalue | 本徵態 / 本徵值 |
| eigenfunction | 本徵函數 |
| Hamiltonian | 哈密頓量 |
| operator | 算符 |
| observable | 可觀測量 |
| commutator | 對易子 |
| anticommutator | 反對易子 |
| momentum / angular momentum | 動量 / 角動量 |
| orbital / total angular momentum | 軌道 / 總角動量 |
| wave function | 波函數 |
| Hilbert space | 希爾伯特空間 |
| ket / bra | ket / bra（不翻；物理慣例） |
| Hermitian | 厄米 |
| unitary | 么正 |
| antiunitary | 反么正 |
| expectation value | 期望值 |
| matrix element | 矩陣元 |
| uncertainty | 不確定性 |
| compatible / incompatible | 相容 / 不相容 |
| basis | 基底 |
| orthonormal | 正規正交 |
| orthogonality | 正交性 |
| projection | 投影 |
| normalization | 歸一化 |
| measurement | 測量 |
| ground / excited state | 基態 / 激發態 |
| bound state | 束縛態 |
| free particle | 自由粒子 |
| quantum number | 量子數 |
| principal / orbital / magnetic quantum number | 主 / 角 / 磁量子數 |

### 動力學（ch02）

| en | zh-TW |
|---|---|
| time evolution | 時間演化 |
| propagator | 傳播子 |
| Schrödinger picture | Schrödinger 繪景 |
| Heisenberg picture | Heisenberg 繪景 |
| interaction picture | 相互作用繪景 |
| equation of motion | 運動方程 |
| translation | 平移 |
| generator | 生成元 |
| creation / annihilation | 產生 / 湮滅 |
| ladder operator | 升降算符 |
| raising / lowering operator | 升 / 降算符 |
| coherent state | 相干態 |
| path integral | 路徑積分 |
| action | 作用量 |
| gauge | 規範 |
| gauge invariance | 規範不變性 |
| gauge transformation | 規範變換 |
| vector / scalar potential | 向量勢 / 純量勢 |
| magnetic / electric field | 磁場 / 電場 |
| magnetic flux | 磁通量 |
| magnetic monopole | 磁單極 |
| Aharonov–Bohm effect | Aharonov–Bohm 效應 |
| harmonic oscillator | 諧振子 |
| wave packet | 波包 |
| canonical / kinetic momentum | 正則動量 / 動能動量 |
| minimal coupling | 最小耦合 |
| curl / gradient / divergence | 旋度 / 梯度 / 散度 |

### 角動量（ch03）

| en | zh-TW |
|---|---|
| rotation | 轉動 |
| rotation matrix | 轉動矩陣 |
| Euler angle | Euler 角 |
| representation | 表示 |
| irreducible | 不可約 |
| tensor | 張量 |
| spherical harmonic | 球諧函數 |
| Clebsch–Gordan coefficient | Clebsch–Gordan 係數 |
| Wigner–Eckart theorem | Wigner–Eckart 定理 |
| addition of angular momenta | 角動量耦合 |
| spinor | 旋量 |
| density matrix | 密度矩陣 |
| pure / mixed state | 純態 / 混合態 |
| polarization | 偏振 |
| ensemble | 系綜 |
| scalar / vector / tensor operator | 純量 / 向量 / 張量算符 |

### 對稱性（ch04）

| en | zh-TW |
|---|---|
| symmetry | 對稱性 |
| conservation law | 守恆律 |
| parity | 宇稱 |
| time reversal | 時間反演 |
| space inversion | 空間反演 |
| lattice | 晶格 |
| Bloch theorem | Bloch 定理 |
| symmetry group | 對稱群 |
| Kramers degeneracy | Kramers 簡併 |
| selection rule | 選擇定則 |
| Berry phase | Berry 相位 |
| discrete / continuous | 離散 / 連續 |
| invariance | 不變性 |
| SO(4) | SO(4) |

### 近似方法（ch05）

| en | zh-TW |
|---|---|
| perturbation | 微擾 |
| time-independent / time-dependent perturbation theory | 定態 / 含時微擾論 |
| degenerate / nondegenerate | 簡併 / 非簡併 |
| unperturbed | 未擾動的 |
| first-order / second-order | 一階 / 二階 |
| energy shift | 能量位移 |
| variational method | 變分法 |
| WKB approximation | WKB 近似 |
| sudden / adiabatic approximation | 突然 / 絕熱近似 |
| Fermi golden rule | Fermi 黃金法則 |
| transition probability | 躍遷機率 |
| dipole approximation | 偶極近似 |
| Stark effect | Stark 效應 |
| Zeeman effect | Zeeman 效應 |
| fine structure | 精細結構 |
| hyperfine | 超精細 |
| spin-orbit coupling | 自旋-軌道耦合 |
| Hartree–Fock | Hartree–Fock |
| coupling constant | 耦合常數 |
| resolvent | 預解算符 |

### 散射（ch06）

| en | zh-TW |
|---|---|
| scattering | 散射 |
| scattering amplitude | 散射振幅 |
| cross section | 截面 |
| differential / total cross section | 微分 / 總截面 |
| Born approximation | Born 近似 |
| partial wave | 分波 |
| partial wave expansion | 分波展開 |
| phase shift | 相移 |
| optical theorem | 光學定理 |
| Lippmann–Schwinger equation | Lippmann–Schwinger 方程 |
| Green's function | Green 函數 |
| T-matrix / S-matrix | T 矩陣 / S 矩陣 |
| resonance | 共振 |
| incident / scattered wave | 入射波 / 散射波 |
| impact parameter | 碰撞參數 |
| center of mass | 質心 |
| laboratory frame | 實驗室參考系 |
| elastic / inelastic | 彈性 / 非彈性 |
| Coulomb / Yukawa / Rutherford scattering | Coulomb / Yukawa / Rutherford 散射 |
| Yukawa potential | Yukawa 位勢 |
| eikonal approximation | eikonal 近似 |
| potential | 位勢 |
| contour | 圍道 |
| pole | 極點 |

### 全同粒子（ch07）

| en | zh-TW |
|---|---|
| identical particle | 全同粒子 |
| indistinguishable / distinguishable | 不可分辨 / 可分辨 |
| symmetric / antisymmetric | 對稱 / 反對稱 |
| permutation | 置換 |
| symmetrizer / antisymmetrizer | 對稱化 / 反對稱化算符 |
| boson / fermion | 玻色子 / 費米子 |
| Pauli exclusion principle | Pauli 不相容原理 |
| Slater determinant | Slater 行列式 |
| occupation number | 佔據數 |
| Fock space | Fock 空間 |
| second quantization | 二次量子化 |
| helium atom | 氦原子 |
| hydrogen molecule | 氫分子 |
| exchange interaction | 交換交互作用 |
| singlet / triplet | 單態 / 三重態 |
| ortho / para | 正 / 仲 |
| quantum statistics | 量子統計 |
| Bose–Einstein condensation | Bose–Einstein 凝聚 |
| degenerate gas | 簡併氣體 |
| anyon | 任意子 |
| braiding | 編織 |

### 相對論量子力學（ch08）

| en | zh-TW |
|---|---|
| relativistic | 相對論性 |
| Klein–Gordon equation | Klein–Gordon 方程 |
| Dirac equation | Dirac 方程 |
| Dirac matrix | Dirac 矩陣 |
| gamma matrix | gamma 矩陣 |
| Weyl spinor | Weyl 旋量 |
| Majorana | Majorana |
| bispinor | 雙旋量 |
| antiparticle | 反粒子 |
| positron / electron | 正電子 / 電子 |
| four-vector / four-momentum | 四維向量 / 四動量 |
| Lorentz transformation | Lorentz 變換 |
| Lorentz invariance | Lorentz 不變性 |
| boost | boost |
| proper time | 固有時 |
| rest frame | 靜止參考系 |
| Minkowski space | Minkowski 空間 |
| Dirac sea | Dirac 海 |
| hole theory | 電洞理論 |
| CPT theorem | CPT 定理 |
| charge conjugation | 電荷共軛 |
| helicity | 螺旋度 |
| chirality | 手徵性 |
| Foldy–Wouthuysen | Foldy–Wouthuysen |
| Zitterbewegung | Zitterbewegung |
| negative energy | 負能量 |

### 附錄

| en | zh-TW |
|---|---|
| Gaussian / SI / Heaviside–Lorentz units | Gaussian / SI / Heaviside–Lorentz 單位制 |
| Maxwell equations | Maxwell 方程組 |
| permittivity / permeability | 介電常數 / 磁導率 |
| analytic function | 解析函數 |
| holomorphic | 全純 |
| Cauchy–Riemann equation | Cauchy–Riemann 方程 |
| residue | 留數 |
| singularity | 奇異點 |
| branch cut | 分支割線 |
| Laurent series | Laurent 級數 |
| Riemann surface | Riemann 曲面 |
| complex plane | 複平面 |
| Hermite / Bessel / Laguerre polynomial | Hermite / Bessel / Laguerre 多項式 |
| radial equation | 徑向方程 |
| separation of variables | 分離變數法 |

### 本書特有（Feynman Vol. III）

| en | zh-TW |
|---|---|
| probability amplitude / amplitude | 機率幅 / 機率幅（Feynman 慣用，勿譯「振幅」） |
| base state | 基底態 |
| two-state system | 雙態系統 |
| ammonia maser | 氨邁射 |
| maser | 邁射 |
| hyperfine splitting | 超精細分裂 |
| flip-flop | 翻轉（自旋交換） |
| crystal lattice | 晶格 |
| propagation in a lattice | 晶格中的傳播 |
| semiconductor | 半導體 |
| donor / acceptor | 施體 / 受體 |
| hole | 電洞 |
| energy band | 能帶 |
| n-type / p-type | n 型 / p 型 |
| independent particle approximation | 獨立粒子近似 |
| paramagnetism | 順磁性 |
| magnetic resonance | 磁共振 |
| superconductivity | 超導 |
| Josephson junction | Josephson 接面 |
| seminar | 研討課 |


## 標點與語氣

- 句末英文標點 `. , ; :` **保留**（不要改全形）
- 中文句子**內部與句末**用全形（。，；：）
- 不加譯註、不擴寫、不刪節
- 不要 markdown fence、不要說明文字
- **不要輸出任何 0x00–0x1f 控制字元**
