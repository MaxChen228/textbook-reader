# Boas《Mathematical Methods in the Physical Sciences》翻譯規範

繁體中文（zh-TW，台灣數學物理慣用語）。本檔由 `/translate-book` skill 派出的 sub-agent 必讀。

## 鐵則（絕對保留原樣）

- LaTeX：`$...$`、`$$...$$`、`\(...\)`、`\[...\]` 整段不動，**包括 `\text{}` / `\mbox{}` / `\mathrm{}` 內的英文**
- 引用編號：`Eq. (1.1)`、`Fig. 1.2`、`Chapter 3`、`Section 2.4`、`Problem 5.7`、`Example 4.2`、`Table 1.1` — 保留英文 + 編號
- 人名：保留英文（Boas、Fourier、Laplace、Legendre、Bessel、Hermite、Laguerre、Gauss、Green、Stokes、Cauchy、Riemann、Taylor、Maclaurin、Euler、Gamma、Dirac、Kronecker、Jacobi、Hamilton、Lagrange、Newton、Cramer、Gram、Schmidt、Sturm、Liouville、Wronski、Frobenius、Poisson、Lorentz、Minkowski…）
- 定理 / 定律第一次出現：原文 + 中譯（例 `Stokes' theorem → Stokes 定理`）；後續直接中譯
- 特殊函數 / 多項式名保留英文 + 中譯（Legendre polynomials → Legendre 多項式；Bessel functions → Bessel 函數；gamma function → gamma 函數）
- 數字、符號、變數保留原樣（`\sin`, `\nabla`, `\partial`, `\Gamma(x)`, `\delta_{ij}`, `e^{i\theta}`, `\sum`, `\int`, `\hbar`）
- 矩陣 / 向量 `\mathbf{...}` / `\vec{...}` 保留原樣
- 圖表 axis label 內嵌 LaTeX 中，不動

## 譯名表

### 級數與微積分

| en | zh-TW |
|---|---|
| infinite series | 無窮級數 |
| power series | 冪級數 |
| convergence | 收斂 |
| divergence | 發散 |
| convergent | 收斂的 |
| divergent | 發散的 |
| radius of convergence | 收斂半徑 |
| interval of convergence | 收斂區間 |
| ratio test | 比值審斂法 |
| comparison test | 比較審斂法 |
| integral test | 積分審斂法 |
| alternating series | 交錯級數 |
| absolute convergence | 絕對收斂 |
| conditional convergence | 條件收斂 |
| Taylor series | Taylor 級數 |
| Maclaurin series | Maclaurin 級數 |
| partial derivative | 偏導數 |
| total differential | 全微分 |
| chain rule | 連鎖律 |
| Lagrange multiplier | Lagrange 乘子 |
| multiple integral | 重積分 |
| Jacobian | Jacobian（雅可比行列式）|
| line integral | 線積分 |
| surface integral | 面積分 |
| volume integral | 體積分 |

### 複數與複變

| en | zh-TW |
|---|---|
| complex number | 複數 |
| real part | 實部 |
| imaginary part | 虛部 |
| modulus | 模 |
| argument | 輻角 |
| complex conjugate | 複共軛 |
| analytic function | 解析函數 |
| Cauchy-Riemann equations | Cauchy-Riemann 方程 |
| residue | 留數 |
| pole | 極點 |
| branch point | 分支點 |
| branch cut | 分支切割 |
| contour integral | 周線積分 |
| Laurent series | Laurent 級數 |

### 線性代數與向量

| en | zh-TW |
|---|---|
| linear algebra | 線性代數 |
| matrix | 矩陣 |
| determinant | 行列式 |
| eigenvalue | 特徵值 |
| eigenvector | 特徵向量 |
| linear transformation | 線性變換 |
| vector space | 向量空間 |
| basis | 基底 |
| linear independence | 線性獨立 |
| orthogonal | 正交 |
| orthonormal | 單範正交 |
| inner product | 內積 |
| cross product | 外積（向量積）|
| dot product | 點積（純量積）|
| gradient | 梯度 |
| divergence | 散度 |
| curl | 旋度 |
| Laplacian | Laplacian（拉普拉斯算子）|
| vector analysis | 向量分析 |
| scalar field | 純量場 |
| vector field | 向量場 |

### 微分方程與特殊函數

| en | zh-TW |
|---|---|
| ordinary differential equation | 常微分方程 (ODE) |
| partial differential equation | 偏微分方程 (PDE) |
| boundary condition | 邊界條件 |
| initial condition | 初始條件 |
| homogeneous | 齊次 |
| inhomogeneous / nonhomogeneous | 非齊次 |
| general solution | 通解 |
| particular solution | 特解 |
| separation of variables | 分離變數 |
| series solution | 級數解 |
| recursion relation | 遞迴關係 |
| generating function | 生成函數 |
| orthogonality | 正交性 |
| Sturm-Liouville | Sturm-Liouville |
| special function | 特殊函數 |
| gamma function | gamma 函數 |
| beta function | beta 函數 |
| error function | 誤差函數 |
| Legendre polynomial | Legendre 多項式 |
| Bessel function | Bessel 函數 |
| Hermite polynomial | Hermite 多項式 |
| Laguerre polynomial | Laguerre 多項式 |
| spherical harmonics | 球諧函數 |

### Fourier / 變換 / 機率

| en | zh-TW |
|---|---|
| Fourier series | Fourier 級數 |
| Fourier transform | Fourier 變換 |
| Laplace transform | Laplace 變換 |
| coefficient | 係數 |
| harmonic | 諧波 |
| periodic function | 週期函數 |
| even function | 偶函數 |
| odd function | 奇函數 |
| Dirac delta function | Dirac delta 函數 |
| convolution | 摺積 |
| probability | 機率 |
| random variable | 隨機變數 |
| probability distribution | 機率分布 |
| expectation value | 期望值 |
| variance | 變異數 |
| standard deviation | 標準差 |
| binomial distribution | 二項分布 |
| normal distribution | 常態分布 |
| Poisson distribution | Poisson 分布 |

### 張量

| en | zh-TW |
|---|---|
| tensor | 張量 |
| tensor analysis | 張量分析 |
| contravariant | 逆變 |
| covariant | 共變 |
| index | 指標 |
| summation convention | 求和約定 |
| metric tensor | 度規張量 |
| Kronecker delta | Kronecker delta |
| Levi-Civita symbol | Levi-Civita 符號 |
