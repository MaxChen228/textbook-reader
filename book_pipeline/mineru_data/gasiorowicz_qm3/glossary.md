# Gasiorowicz《Quantum Physics, 3rd》翻譯規範

繁體中文（zh-TW，台灣物理慣用語）。由 `/translate-book` sub-agent 必讀。

## 鐵則（絕對保留原樣）

- LaTeX：`$...$`、`$$...$$`、`\(...\)`、`\[...\]` 整段不動，**含 `\text{}` / `\mathrm{}` 內英文**
- 引用編號：`Eq. (1.1)`、`Fig. 1.2`、`Section 2.3`、`Chapter 1`、`Problem 1.5` — 保留英文+編號
- **人名一律保留英文原樣，不音譯**（Schrödinger、Heisenberg、de Broglie、Bohr、Dirac、Pauli、Einstein、Planck、Compton、Born、Bell、Coulomb、Bessel、Hermite、Legendre…）。譯「德布羅意」「薛丁格」即為錯誤
- 理論/實驗首次出現：原文+中譯（`Stern–Gerlach experiment → Stern–Gerlach 實驗`）；後續直接中譯
- 數字、單位（`10 T/m`、`\hbar`）、變數名（`s_z`、`H_0`、`|j,m>`）保留原樣
- **古典**（classical），非「經典」；**透射**（transmitted），非「穿過」

## 譯名表

### 通用
| en | zh-TW |
|---|---|
| spin | 自旋 |
| eigenstate / eigenvalue / eigenfunction | 本徵態 / 本徵值 / 本徵函數 |
| Hamiltonian | 哈密頓量 |
| operator | 算符 |
| observable | 可觀測量 |
| commutator / anticommutator | 對易子 / 反對易子 |
| momentum / angular momentum | 動量 / 角動量 |
| orbital / total angular momentum | 軌道 / 總角動量 |
| wave function | 波函數 |
| Hilbert space | 希爾伯特空間 |
| ket / bra | ket / bra（不翻；物理慣例） |
| Hermitian | 厄米 |
| unitary / antiunitary | 么正 / 反么正 |
| expectation value | 期望值 |
| matrix element | 矩陣元 |
| uncertainty | 不確定性 |
| compatible / incompatible | 相容 / 不相容 |
| basis | 基底 |
| orthonormal / orthogonality | 正規正交 / 正交性 |
| projection | 投影 |
| normalization | 歸一化 |
| measurement | 測量 |
| ground / excited state | 基態 / 激發態 |
| bound state / free particle | 束縛態 / 自由粒子 |
| quantum number | 量子數 |
| principal / orbital / magnetic quantum number | 主 / 角 / 磁量子數 |
| classical limit | 古典極限 |

### 舊量子論（ch1）
| en | zh-TW |
|---|---|
| blackbody radiation | 黑體輻射 |
| photoelectric effect | 光電效應 |
| Compton scattering | Compton 散射 |
| de Broglie wavelength | de Broglie 波長 |
| Bohr model | Bohr 模型 |
| correspondence principle | 對應原理 |
| wave-particle duality | 波粒二象性 |
| matter wave | 物質波 |
| quantization | 量子化 |
| photon | 光子 |

### 波動力學
| en | zh-TW |
|---|---|
| Schrödinger equation | Schrödinger 方程 |
| wave packet | 波包 |
| probability density / current | 機率密度 / 機率流 |
| potential | 位勢 |
| potential barrier / well | 位壘 / 位阱 |
| transmission / reflection coefficient | 透射 / 反射係數 |
| tunneling | 穿隧 |
| flux | 通量 |
| harmonic oscillator | 諧振子 |
| expansion postulate | 展開假設 |
| completeness | 完備性 |
| degeneracy | 簡併 |

### 算符方法 / 角動量
| en | zh-TW |
|---|---|
| ladder / raising / lowering operator | 升降 / 升 / 降算符 |
| creation / annihilation | 產生 / 湮滅 |
| coherent state | 相干態 |
| rotation | 轉動 |
| spherical harmonic | 球諧函數 |
| Clebsch–Gordan coefficient | Clebsch–Gordan 係數 |
| addition of angular momenta | 角動量耦合 |
| spinor | 旋量 |
| representation | 表示 |

### 氫原子 / 微擾
| en | zh-TW |
|---|---|
| radial equation | 徑向方程 |
| separation of variables | 分離變數法 |
| perturbation | 微擾 |
| time-independent / time-dependent perturbation theory | 定態 / 含時微擾論 |
| degenerate / nondegenerate | 簡併 / 非簡併 |
| first-order / second-order | 一階 / 二階 |
| energy shift | 能量位移 |
| variational method | 變分法 |
| WKB approximation | WKB 近似 |
| fine structure | 精細結構 |
| hyperfine | 超精細 |
| spin-orbit coupling | 自旋-軌道耦合 |
| Stark / Zeeman effect | Stark / Zeeman 效應 |
| Fermi golden rule | Fermi 黃金法則 |
| transition probability | 躍遷機率 |
| selection rule | 選擇定則 |

### 多粒子 / 原子分子
| en | zh-TW |
|---|---|
| identical particle | 全同粒子 |
| indistinguishable / distinguishable | 不可分辨 / 可分辨 |
| symmetric / antisymmetric | 對稱 / 反對稱 |
| boson / fermion | 玻色子 / 費米子 |
| Pauli exclusion principle | Pauli 不相容原理 |
| Slater determinant | Slater 行列式 |
| exchange interaction | 交換交互作用 |
| singlet / triplet | 單態 / 三重態 |
| helium atom / hydrogen molecule | 氦原子 / 氫分子 |
| screening | 屏蔽 |

### 輻射 / 散射
| en | zh-TW |
|---|---|
| radiative decay | 輻射衰變 |
| spontaneous / stimulated emission | 自發 / 受激輻射 |
| dipole approximation | 偶極近似 |
| electromagnetic field | 電磁場 |
| vector / scalar potential | 向量勢 / 純量勢 |
| gauge / gauge invariance | 規範 / 規範不變性 |
| minimal coupling | 最小耦合 |
| scattering | 散射 |
| scattering amplitude | 散射振幅 |
| cross section | 截面 |
| differential / total cross section | 微分 / 總截面 |
| Born approximation | Born 近似 |
| partial wave / phase shift | 分波 / 相移 |
| optical theorem | 光學定理 |
| resonance | 共振 |
| elastic / inelastic | 彈性 / 非彈性 |
| Yukawa potential | Yukawa 位勢 |

### 糾纏（ch20）
| en | zh-TW |
|---|---|
| entanglement | 糾纏 |
| Bell inequality | Bell 不等式 |
| EPR paradox | EPR 悖論 |
| hidden variable | 隱變數 |
| density matrix | 密度矩陣 |
| pure / mixed state | 純態 / 混合態 |
| qubit | 量子位元 |
| decoherence | 退相干 |

## 標點與語氣

- 句中/句末中文用全形（。，；：）；行內英文標點 `. , ; :` 保留半形
- 不加譯註、不擴寫、不刪節、不要 markdown fence、不要說明文字
- **不要輸出任何 0x00–0x1f 控制字元**
