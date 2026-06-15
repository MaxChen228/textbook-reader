# Griffiths《Introduction to Electrodynamics, 4th》翻譯規範

繁體中文（zh-TW，台灣物理慣用語）。本檔由 `/translate-book` skill 派出的 sub-agent 必讀。

## 鐵則（絕對保留原樣）

- LaTeX：`$...$`、`$$...$$`、`\(...\)`、`\[...\]` 整段不動，**包括 `\text{}` / `\mbox{}` / `\mathrm{}` 內的英文**
- 引用編號：`Eq. (1.1)`、`Fig. 1.2`、`Chapter 3`、`Section 2.4`、`Problem 5.7`、`Example 4.2`、`Table 1.1` — 保留英文 + 編號
- 人名：保留英文（Griffiths、Maxwell、Faraday、Ampère、Gauss、Coulomb、Newton、Lorentz、Einstein、Poisson、Laplace、Helmholtz、Stokes、Green、Dirac、Heaviside、Hertz、Larmor、Liénard、Wiechert、Poynting、Biot、Savart、Lenz、Kirchhoff、Ohm、Snell、Fresnel、Brewster、Rayleigh、Thomson、Cherenkov、Doppler、Minkowski、Fourier、Bessel、Legendre…）
- 定理 / 定律 / 方程第一次出現：原文 + 中譯（例 `Stokes' theorem → Stokes 定理`）；後續直接中譯
- 數字、單位（`3 m`, `1 C`, `\hbar`, `eV`, `T`, `Wb`, `V/m`）保留原樣
- 程式碼 / 變數名（`E`, `B`, `D`, `H`, `\rho`, `\sigma`, `\mathbf{J}`, `\Phi`, `\mathbf{A}`, `c`, `\epsilon_0`, `\mu_0`）保留原樣
- 圖表 axis label 內嵌 LaTeX 中，不動
- 向量 `\mathbf{...}` / `\vec{...}` 保留原樣

## 譯名表

### 基本電磁學量

| en | zh-TW |
|---|---|
| electric charge | 電荷 |
| electric field | 電場 |
| magnetic field | 磁場 |
| electric flux | 電通量 |
| magnetic flux | 磁通量 |
| electric potential | 電位 / 電勢 |
| magnetic vector potential | 磁向量位 |
| scalar potential | 純量位 |
| vector potential | 向量位 |
| voltage | 電壓 |
| current | 電流 |
| current density | 電流密度 |
| surface current density | 面電流密度 |
| volume current density | 體電流密度 |
| charge density | 電荷密度 |
| surface charge density | 面電荷密度 |
| volume charge density | 體電荷密度 |
| line charge density | 線電荷密度 |
| electric dipole moment | 電偶極矩 |
| magnetic dipole moment | 磁偶極矩 |
| polarization | 極化（強度） |
| magnetization | 磁化（強度） |
| displacement | 電位移 |
| auxiliary field H | 輔助場 H |
| permittivity | 電容率 / 介電常數 |
| permeability | 磁導率 |
| dielectric constant | 介電常數 |
| susceptibility | 感受率 |
| electric susceptibility | 電感受率 |
| magnetic susceptibility | 磁感受率 |
| conductivity | 電導率 |
| resistivity | 電阻率 |
| capacitance | 電容 |
| inductance | 電感 |
| mutual inductance | 互感 |
| self-inductance | 自感 |
| resistance | 電阻 |
| emf | 電動勢 |
| electromotive force | 電動勢 |

### 向量分析（ch1 / appA）

| en | zh-TW |
|---|---|
| vector analysis | 向量分析 |
| scalar | 純量 |
| vector | 向量 |
| unit vector | 單位向量 |
| vector field | 向量場 |
| scalar field | 純量場 |
| dot product | 內積 / 點積 |
| cross product | 外積 / 叉積 |
| triple product | 三重積 |
| gradient | 梯度 |
| divergence | 散度 |
| curl | 旋度 |
| Laplacian | Laplacian / 拉氏算子 |
| del operator | del 算子 / nabla |
| line integral | 線積分 |
| surface integral | 面積分 |
| volume integral | 體積分 |
| fundamental theorem | 基本定理 |
| divergence theorem | 散度定理 |
| Stokes' theorem | Stokes 定理 |
| Green's theorem | Green 定理 |
| curvilinear coordinates | 曲線座標 |
| Cartesian coordinates | 直角座標 |
| spherical coordinates | 球座標 |
| cylindrical coordinates | 圓柱座標 |
| Jacobian | Jacobian |
| Dirac delta function | Dirac delta 函數 |
| solid angle | 立體角 |
| position vector | 位置向量 |
| separation vector | 分離向量 |
| source point / field point | 源點 / 場點 |

### 靜電學（ch2, ch3）

| en | zh-TW |
|---|---|
| electrostatics | 靜電學 |
| Coulomb's law | Coulomb 定律 |
| superposition principle | 疊加原理 |
| Gauss's law | Gauss 定律 |
| Gaussian surface | Gauss 面 |
| flux | 通量 |
| field line | 場線 |
| equipotential | 等位面 |
| conductor | 導體 |
| insulator | 絕緣體 |
| grounded | 接地 |
| image charge | 鏡像電荷 |
| method of images | 鏡像法 |
| Poisson equation | Poisson 方程 |
| Laplace equation | Laplace 方程 |
| boundary condition | 邊界條件 |
| boundary value problem | 邊界值問題 |
| Dirichlet / Neumann | Dirichlet / Neumann |
| uniqueness theorem | 唯一性定理 |
| separation of variables | 分離變數法 |
| Legendre polynomial | Legendre 多項式 |
| Bessel function | Bessel 函數 |
| multipole expansion | 多極展開 |
| monopole | 單極 |
| dipole | 偶極 |
| quadrupole | 四極 |
| dipole moment | 偶極矩 |
| work and energy | 功與能 |
| electrostatic energy | 靜電能 |
| energy density | 能量密度 |

### 物質中的電磁場（ch4, ch6）

| en | zh-TW |
|---|---|
| dielectric | 介電質 |
| linear dielectric | 線性介電質 |
| bound charge | 束縛電荷 |
| free charge | 自由電荷 |
| bound current | 束縛電流 |
| free current | 自由電流 |
| polarization charge | 極化電荷 |
| magnetization current | 磁化電流 |
| paramagnet | 順磁體 |
| diamagnet | 抗磁體 |
| ferromagnet | 鐵磁體 |
| hysteresis | 磁滯 |
| domain | 磁區 / 疇 |
| saturation | 飽和 |
| linear medium | 線性介質 |
| isotropic | 各向同性 |
| anisotropic | 各向異性 |

### 磁靜學（ch5）

| en | zh-TW |
|---|---|
| magnetostatics | 磁靜學 |
| Biot–Savart law | Biot–Savart 定律 |
| Ampère's law | Ampère 定律 |
| Amperian loop | Amperian 環路 |
| solenoid | 螺線管 |
| toroid | 螺絞管 / 環形線圈 |
| Lorentz force | Lorentz 力 |
| magnetic force | 磁力 |
| cyclotron motion | 迴旋運動 |
| cyclotron frequency | 迴旋頻率 |
| Hall effect | Hall 效應 |
| magnetic monopole | 磁單極 |

### 電動力學（ch7）

| en | zh-TW |
|---|---|
| electrodynamics | 電動力學 |
| Ohm's law | Ohm 定律 |
| electromotive force (emf) | 電動勢 |
| motional emf | 動生電動勢 |
| Faraday's law | Faraday 定律 |
| Lenz's law | Lenz 定律 |
| induced emf | 感應電動勢 |
| induced electric field | 感應電場 |
| mutual induction | 互感 |
| self-induction | 自感 |
| inductor | 電感器 |
| transformer | 變壓器 |
| eddy current | 渦電流 |
| Maxwell's equations | Maxwell 方程組 |
| displacement current | 位移電流 |
| continuity equation | 連續方程 |
| magnetic charge | 磁荷 |

### 守恆律 / 電磁波（ch8, ch9）

| en | zh-TW |
|---|---|
| conservation law | 守恆律 |
| Poynting vector | Poynting 向量 |
| Poynting's theorem | Poynting 定理 |
| energy flux | 能流 |
| momentum density | 動量密度 |
| Maxwell stress tensor | Maxwell 應力張量 |
| angular momentum | 角動量 |
| electromagnetic wave | 電磁波 |
| wave equation | 波動方程 |
| plane wave | 平面波 |
| spherical wave | 球面波 |
| polarization (of wave) | 偏振 |
| linear polarization | 線偏振 |
| circular polarization | 圓偏振 |
| elliptical polarization | 橢圓偏振 |
| wavelength | 波長 |
| frequency | 頻率 |
| angular frequency | 角頻率 |
| wave number | 波數 |
| wave vector | 波向量 |
| phase | 相位 |
| phase velocity | 相速度 |
| group velocity | 群速度 |
| index of refraction | 折射率 |
| dispersion | 色散 |
| reflection | 反射 |
| refraction | 折射 |
| transmission | 透射 |
| Snell's law | Snell 定律 |
| Fresnel equations | Fresnel 方程 |
| Brewster angle | Brewster 角 |
| total internal reflection | 全反射 |
| evanescent wave | 衰逝波 |
| absorption | 吸收 |
| skin depth | 趨膚深度 |
| guided wave | 導波 |
| waveguide | 波導 |
| transmission line | 傳輸線 |
| TE / TM mode | TE / TM 模 |
| cutoff frequency | 截止頻率 |

### 位與輻射（ch10, ch11）

| en | zh-TW |
|---|---|
| scalar potential | 純量位 |
| vector potential | 向量位 |
| gauge | 規範 |
| gauge transformation | 規範變換 |
| Coulomb gauge | Coulomb 規範 |
| Lorenz gauge | Lorenz 規範 |
| retarded potential | 推遲位 |
| advanced potential | 超前位 |
| retarded time | 推遲時間 |
| Liénard–Wiechert potential | Liénard–Wiechert 位 |
| radiation | 輻射 |
| dipole radiation | 偶極輻射 |
| electric dipole radiation | 電偶極輻射 |
| magnetic dipole radiation | 磁偶極輻射 |
| Larmor formula | Larmor 公式 |
| radiation resistance | 輻射電阻 |
| radiation reaction | 輻射反作用 |
| bremsstrahlung | 制動輻射 |
| synchrotron radiation | 同步輻射 |
| Cherenkov radiation | Cherenkov 輻射 |
| antenna | 天線 |

### 相對論電動力學（ch12）

| en | zh-TW |
|---|---|
| special relativity | 狹義相對論 |
| postulates of relativity | 相對論假設 |
| inertial frame | 慣性系 |
| reference frame | 參考系 |
| Lorentz transformation | Lorentz 變換 |
| Galilean transformation | Galilean 變換 |
| relativity of simultaneity | 同時性的相對性 |
| time dilation | 時間膨脹 |
| length contraction | 長度收縮 |
| proper time | 固有時 |
| proper length | 固有長度 |
| spacetime | 時空 |
| Minkowski diagram | Minkowski 圖 |
| worldline | 世界線 |
| light cone | 光錐 |
| invariant interval | 不變間隔 |
| four-vector | 四向量 |
| four-velocity | 四速度 |
| four-momentum | 四動量 |
| four-current | 四電流 |
| four-potential | 四位 |
| relativistic energy | 相對論能量 |
| relativistic momentum | 相對論動量 |
| rest mass | 靜質量 |
| rest energy | 靜能 |
| relativistic Doppler effect | 相對論 Doppler 效應 |
| field tensor | 場張量 |
| dual tensor | 對偶張量 |
| covariant / contravariant | 協變 / 逆變 |
| metric tensor | 度規張量 |
| tensor | 張量 |

### 通用詞

| en | zh-TW |
|---|---|
| problem | 習題 |
| example | 例題 |
| solution | 解答 |
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
| symmetry | 對稱性 |
| approximation | 近似 |
| boundary | 邊界 |
| surface | 表面 / 曲面 |
| volume | 體積 |
| sphere / spherical | 球 / 球形 |
| cylinder / cylindrical | 圓柱 / 圓柱形 |
| plane / planar | 平面 / 平面的 |
| infinite / semi-infinite | 無限 / 半無限 |
| uniform | 均勻 |
| static / steady | 靜態 / 穩態 |

## 標點與語氣

- 句末英文標點 `. , ; :` **保留**（不要改全形）
- 中文句子**內部與句末**用全形（。，；：）
- 不加譯註、不擴寫、不刪節
- 不要 markdown fence、不要說明文字
- **不要輸出任何 0x00–0x1f 控制字元**

## Cheng EM 補充術語（transmission lines / waveguides / antennas）

- transmission line — 傳輸線
- characteristic impedance — 特性阻抗
- standing wave ratio (SWR) — 駐波比
- reflection coefficient — 反射係數
- Smith chart — 史密斯圖
- stub matching — 短截線匹配
- waveguide — 波導
- TE mode / TM mode / TEM mode — TE 模 / TM 模 / TEM 模
- cutoff frequency — 截止頻率
- cavity resonator — 諧振腔
- quality factor (Q) — 品質因數
- antenna — 天線
- radiation pattern — 輻射場型
- radiation resistance — 輻射電阻
- directivity — 方向性
- gain — 增益
- effective aperture — 有效孔徑
- dipole antenna — 偶極天線
- array antenna — 陣列天線
- far field / near field — 遠場 / 近場
- retarded potential — 推遲位勢
- Hertzian dipole — 赫茲偶極
- Friis transmission formula — Friis 傳輸公式
