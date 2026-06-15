# Kittel & Kroemer《Thermal Physics, 2nd》翻譯規範

繁體中文（zh-TW，台灣物理慣用語）。本檔由 `/translate-book` skill 派出的 sub-agent 必讀。

## 鐵則（絕對保留原樣）

- LaTeX：`$...$`、`$$...$$`、`\(...\)`、`\[...\]` 整段不動，**包括 `\text{}` / `\mbox{}` / `\mathrm{}` 內的英文**
- 引用編號：`Eq. (1.1)`、`Fig. 1.2`、`Chapter 3`、`Section 2.4`、`Problem 5.7`、`Table 1.1` — 保留英文 + 編號
- 人名：保留英文（Kittel、Kroemer、Boltzmann、Maxwell、Gibbs、Helmholtz、Planck、Einstein、Bose、Fermi、Dirac、Debye、Carnot、Clausius、Joule、Kelvin、Onsager、Chandrasekhar、Avogadro、Stirling、Stefan、Wien、Rayleigh、Jeans、Sommerfeld、van der Waals、Clapeyron、Le Chatelier、Nernst、Arrhenius、Langmuir、Dulong、Petit、Lindemann、London、Heisenberg、Schrödinger…）
- 理論/實驗/分布/方程第一次出現：原文 + 中譯（例 `Boltzmann distribution → Boltzmann 分布`）；後續直接中譯
- 數字、單位（`300 K`, `1 atm`, `k_B`, `\hbar`, `eV`, `\mu_B`）保留原樣
- 程式碼 / 變數名（`U`, `S`, `F`, `G`, `T`, `\mu`, `Z`, `\beta`）保留原樣
- 圖表內 axis label 若內嵌在 LaTeX 中，不動

## 譯名表

### 基本熱力學量

| en | zh-TW |
|---|---|
| temperature | 溫度 |
| fundamental / thermodynamic temperature | 基本 / 熱力學溫度 |
| absolute temperature | 絕對溫度 |
| heat | 熱 |
| work | 功 |
| internal energy | 內能 |
| energy | 能量 |
| entropy | 熵 |
| enthalpy | 焓 |
| Helmholtz free energy | Helmholtz 自由能 |
| Gibbs free energy | Gibbs 自由能 |
| free energy | 自由能 |
| chemical potential | 化學勢 |
| pressure | 壓力 |
| volume | 體積 |
| heat capacity | 熱容 |
| specific heat | 比熱 |
| compressibility | 壓縮率 |
| thermal expansion | 熱膨脹 |
| equation of state | 狀態方程 |
| extensive / intensive | 廣度量 / 強度量 |
| reversible / irreversible | 可逆 / 不可逆 |
| quasi-static | 準靜態 |
| isothermal | 等溫 |
| isobaric | 等壓 |
| isochoric | 等容 |
| adiabatic | 絕熱 |
| isentropic | 等熵 |

### 統計力學基礎

| en | zh-TW |
|---|---|
| statistical mechanics | 統計力學 |
| ensemble | 系綜 |
| microcanonical ensemble | 微正則系綜 |
| canonical ensemble | 正則系綜 |
| grand canonical ensemble | 巨正則系綜 |
| state | 態 |
| microstate | 微觀態 |
| macrostate | 宏觀態 |
| accessible state | 可及態 |
| quantum state | 量子態 |
| multiplicity | 多重性 |
| degeneracy | 簡併度 |
| partition function | 配分函數 |
| grand partition function | 巨配分函數 |
| Boltzmann factor | Boltzmann 因子 |
| Gibbs factor | Gibbs 因子 |
| fluctuation | 漲落 |
| thermal average | 熱平均 |
| ensemble average | 系綜平均 |
| equilibrium | 平衡 |
| thermal equilibrium | 熱平衡 |
| reservoir | 熱庫 |
| heat reservoir | 熱庫 |
| thermal contact | 熱接觸 |
| diffusive contact | 擴散接觸 |
| isolated system | 孤立系統 |
| closed system | 封閉系統 |
| open system | 開放系統 |
| degree of freedom | 自由度 |
| equipartition | 均分 |
| equipartition theorem | 能量均分定理 |
| Stirling approximation | Stirling 近似 |

### 分布與分布函數

| en | zh-TW |
|---|---|
| distribution | 分布 |
| Boltzmann distribution | Boltzmann 分布 |
| Planck distribution | Planck 分布 |
| Bose–Einstein distribution | Bose–Einstein 分布 |
| Fermi–Dirac distribution | Fermi–Dirac 分布 |
| Maxwell distribution | Maxwell 分布 |
| Maxwell–Boltzmann distribution | Maxwell–Boltzmann 分布 |
| Gibbs distribution | Gibbs 分布 |
| Poisson distribution | Poisson 分布 |
| velocity distribution | 速度分布 |
| speed distribution | 速率分布 |
| density of states | 態密度 |
| occupancy | 佔據（數） |
| occupation number | 佔據數 |
| mean occupation | 平均佔據 |
| Fermi level / Fermi energy | Fermi 能階 / Fermi 能 |
| Fermi temperature | Fermi 溫度 |
| Fermi velocity / momentum | Fermi 速度 / 動量 |
| Fermi sphere / surface | Fermi 球 / 面 |
| chemical potential | 化學勢 |
| activity | 活度 |
| fugacity | 逸度 |

### 氣體與粒子

| en | zh-TW |
|---|---|
| ideal gas | 理想氣體 |
| classical gas | 古典氣體 |
| ideal classical gas | 理想古典氣體 |
| quantum gas | 量子氣體 |
| degenerate gas | 簡併氣體 |
| Fermi gas | Fermi 氣體 |
| Bose gas | Bose 氣體 |
| Bose–Einstein condensation | Bose–Einstein 凝聚 |
| photon | 光子 |
| phonon | 聲子 |
| boson / fermion | 玻色子 / 費米子 |
| identical particle | 全同粒子 |
| indistinguishable | 不可分辨 |
| Pauli exclusion principle | Pauli 不相容原理 |
| thermal de Broglie wavelength | 熱 de Broglie 波長 |
| quantum concentration | 量子濃度 |
| mean free path | 平均自由徑 |
| collision | 碰撞 |
| cross section | 截面 |
| number density | 粒子數密度 |
| particle number | 粒子數 |
| concentration | 濃度 |

### 輻射與固體

| en | zh-TW |
|---|---|
| thermal radiation | 熱輻射 |
| blackbody radiation | 黑體輻射 |
| cavity radiation | 空腔輻射 |
| Planck radiation law | Planck 輻射定律 |
| Stefan–Boltzmann law | Stefan–Boltzmann 定律 |
| Wien displacement law | Wien 位移定律 |
| Rayleigh–Jeans law | Rayleigh–Jeans 定律 |
| spectral density | 光譜密度 |
| solid | 固體 |
| crystal | 晶體 |
| lattice vibration | 晶格振動 |
| Debye model | Debye 模型 |
| Debye temperature | Debye 溫度 |
| Einstein model | Einstein 模型 |
| Dulong–Petit law | Dulong–Petit 定律 |
| heat capacity of solid | 固體熱容 |

### 相變與化學熱力學

| en | zh-TW |
|---|---|
| phase | 相 |
| phase transformation / transition | 相變 |
| first-order / second-order transition | 一級 / 二級相變 |
| coexistence | 共存 |
| coexistence curve | 共存曲線 |
| phase diagram | 相圖 |
| triple point | 三相點 |
| critical point | 臨界點 |
| critical temperature | 臨界溫度 |
| latent heat | 潛熱 |
| Clausius–Clapeyron equation | Clausius–Clapeyron 方程 |
| van der Waals equation | van der Waals 方程 |
| vapor pressure | 蒸氣壓 |
| sublimation | 昇華 |
| condensation | 凝結 |
| evaporation | 蒸發 |
| melting / freezing | 熔化 / 凝固 |
| nucleation | 成核 |
| supersaturation | 過飽和 |
| superconductor | 超導體 |
| superfluid | 超流體 |
| ferromagnet | 鐵磁體 |
| chemical reaction | 化學反應 |
| equilibrium constant | 平衡常數 |
| law of mass action | 質量作用定律 |
| binary mixture | 雙元混合物 |
| solution | 溶液 |
| solute / solvent | 溶質 / 溶劑 |
| ideal mixture | 理想混合物 |
| mole fraction | 莫耳分率 |
| osmotic pressure | 滲透壓 |
| miscibility / immiscibility | 互溶 / 不互溶 |
| solubility | 溶解度 |

### 低溫物理（ch12）

| en | zh-TW |
|---|---|
| cryogenics | 低溫學 |
| refrigeration | 製冷 |
| refrigerator | 冰箱 / 製冷機 |
| Joule–Thomson effect | Joule–Thomson 效應 |
| throttling | 節流 |
| inversion temperature | 反演溫度 |
| liquefaction | 液化 |
| adiabatic demagnetization | 絕熱去磁 |
| dilution refrigerator | 稀釋製冷機 |
| Carnot cycle / engine | Carnot 循環 / 引擎 |
| heat engine | 熱機 |
| heat pump | 熱泵 |
| efficiency | 效率 |
| coefficient of performance | 性能係數 |

### 半導體（ch13）

| en | zh-TW |
|---|---|
| semiconductor | 半導體 |
| intrinsic / extrinsic | 本徵 / 非本徵 |
| n-type / p-type | n 型 / p 型 |
| donor / acceptor | 施體 / 受體 |
| band gap | 能隙 |
| conduction band | 導帶 |
| valence band | 價帶 |
| hole | 電洞 |
| carrier | 載子 |
| effective mass | 有效質量 |
| mobility | 遷移率 |
| doping | 摻雜 |
| impurity | 雜質 |
| ionization | 離化 |
| p-n junction | p-n 接面 |
| depletion region | 空乏區 |
| Fermi level pinning | Fermi 能階釘紮 |

### 動力論與輸運（ch14, ch15）

| en | zh-TW |
|---|---|
| kinetic theory | 動力論 |
| transport | 輸運 |
| diffusion | 擴散 |
| diffusion coefficient | 擴散係數 |
| thermal conductivity | 熱傳導率 |
| viscosity | 黏滯性 / 黏度 |
| electrical conductivity | 電導率 |
| Ohm's law | Ohm 定律 |
| Fourier law | Fourier 定律 |
| Fick's law | Fick 定律 |
| relaxation time | 弛豫時間 |
| Boltzmann equation | Boltzmann 方程 |
| collision integral | 碰撞積分 |
| drift velocity | 漂移速度 |
| propagation | 傳播 |
| sound wave | 聲波 |
| diffusion equation | 擴散方程 |
| current density | 電流密度 |
| mobility | 遷移率 |
| Einstein relation | Einstein 關係 |

### 附錄

| en | zh-TW |
|---|---|
| Gaussian integral | Gaussian 積分 |
| temperature scale | 溫標 |
| Kelvin scale | Kelvin 溫標 |
| Celsius scale | 攝氏溫標 |
| International Temperature Scale | 國際溫標 |
| Poisson distribution | Poisson 分布 |
| negative temperature | 負溫度 |
| population inversion | 居量反轉 |

## 標點與語氣

- 句末英文標點 `. , ; :` **保留**（不要改全形）
- 中文句子**內部與句末**用全形（。，；：）
- 不加譯註、不擴寫、不刪節
- 不要 markdown fence、不要說明文字
- **不要輸出任何 0x00–0x1f 控制字元**
