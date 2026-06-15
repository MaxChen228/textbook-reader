# Sedra & Smith《Microelectronic Circuits》翻譯規範

繁體中文（zh-TW，台灣電子電路慣用語）。本檔由 `/translate-book` skill 派出的 sub-agent 必讀。

## 鐵則（絕對保留原樣）

- LaTeX：`$...$`、`$$...$$`、`\(...\)`、`\[...\]` 整段不動，**包括 `\text{}` / `\mbox{}` / `\mathrm{}` 內的英文**
- 引用編號：`Eq. (1.1)`、`Fig. 1.2`、`Chapter 3`、`Section 2.4`、`Problem 5.7`、`Example 4.2`、`Table 1.1` — 保留英文 + 編號
- 人名：保留英文（Sedra、Smith、Ohm、Kirchhoff、Thévenin、Norton、Bode、Miller、Nyquist、Shockley、Boltzmann、Fourier、Laplace、Maxwell、Early、Gummel、Ebers、Moll…）
- 元件 / 製程縮寫保留英文：BJT、MOSFET、JFET、CMOS、NMOS、PMOS、FET、IC、op amp / opamp、diode、LED、CMRR、PSRR、GBW、SR（slew rate）、ADC、DAC、SRAM、DRAM、ROM、PLL、TTL、ECL、RTL
- 端點 / 節點名保留英文：gate、drain、source、body（substrate）、base、collector、emitter；G、D、S、B、C、E
- 數字、單位（`5 V`, `1 mA`, `10 k\Omega`, `1 \mu F`, `1 \text{GHz}`, `dB`, `dBm`）保留原樣
- 變數 / 符號保留原樣（`v_{GS}`, `i_D`, `V_T`, `g_m`, `r_o`, `\beta`, `\mu_n C_{ox}`, `V_{OV}`, `V_A`, `f_T`, `\omega_0`, `Q`）
- 定理 / 定律 / 方程第一次出現：原文 + 中譯（例 `Kirchhoff's current law → Kirchhoff 電流定律 (KCL)`）；後續直接中譯
- 圖表 axis label 內嵌 LaTeX 中，不動

## 譯名表

### 基本量與訊號

| en | zh-TW |
|---|---|
| voltage | 電壓 |
| current | 電流 |
| resistance | 電阻 |
| conductance | 電導 |
| capacitance | 電容 |
| inductance | 電感 |
| impedance | 阻抗 |
| admittance | 導納 |
| power | 功率 |
| signal | 訊號 |
| analog signal | 類比訊號 |
| digital signal | 數位訊號 |
| frequency | 頻率 |
| frequency spectrum | 頻譜 |
| amplitude | 振幅 |
| phase | 相位 |
| bandwidth | 頻寬 |
| gain | 增益 |
| open-loop gain | 開迴路增益 |
| closed-loop gain | 閉迴路增益 |
| loop gain | 迴路增益 |
| node | 節點 |
| branch | 分支 |
| ground | 接地 |
| terminal | 端點 |
| port | 埠 |

### 半導體 / 元件

| en | zh-TW |
|---|---|
| semiconductor | 半導體 |
| intrinsic semiconductor | 本質半導體 |
| extrinsic semiconductor | 外質半導體 |
| doping | 摻雜 |
| donor | 施體 |
| acceptor | 受體 |
| carrier | 載子 |
| electron | 電子 |
| hole | 電洞 |
| majority carrier | 多數載子 |
| minority carrier | 少數載子 |
| drift | 漂移 |
| diffusion | 擴散 |
| recombination | 復合 |
| depletion region | 空乏區 |
| pn junction | pn 接面 |
| forward bias | 順向偏壓 |
| reverse bias | 逆向偏壓 |
| breakdown | 崩潰 |
| diode | 二極體 |
| rectifier | 整流器 |
| zener diode | 齊納二極體 |
| transistor | 電晶體 |
| bipolar junction transistor | 雙極接面電晶體 (BJT) |
| field-effect transistor | 場效電晶體 (FET) |
| threshold voltage | 臨界電壓 |
| transconductance | 轉導 |
| channel | 通道 |
| pinch-off | 夾止 |
| saturation region | 飽和區 |
| triode region | 三極管區 |
| cutoff region | 截止區 |
| active region | 主動區 |

### 放大器 / 電路

| en | zh-TW |
|---|---|
| amplifier | 放大器 |
| operational amplifier | 運算放大器 |
| differential amplifier | 差動放大器 |
| common-source | 共源極 |
| common-gate | 共閘極 |
| common-drain | 共汲極（源極隨耦器）|
| common-emitter | 共射極 |
| common-base | 共基極 |
| common-collector | 共集極（射極隨耦器）|
| source follower | 源極隨耦器 |
| emitter follower | 射極隨耦器 |
| current mirror | 電流鏡 |
| current source | 電流源 |
| voltage source | 電壓源 |
| biasing | 偏壓 |
| operating point | 工作點 |
| quiescent point | 靜態工作點 (Q 點) |
| small-signal | 小訊號 |
| large-signal | 大訊號 |
| equivalent circuit | 等效電路 |
| load line | 負載線 |
| input resistance | 輸入電阻 |
| output resistance | 輸出電阻 |
| feedback | 回授 |
| negative feedback | 負回授 |
| frequency response | 頻率響應 |
| pole | 極點 |
| zero | 零點 |
| gain-bandwidth product | 增益頻寬乘積 |
| slew rate | 迴轉率 |
| offset | 偏移 |
| distortion | 失真 |
| noise | 雜訊 |

### 數位電路

| en | zh-TW |
|---|---|
| logic gate | 邏輯閘 |
| inverter | 反相器 |
| logic level | 邏輯準位 |
| noise margin | 雜訊容限 |
| propagation delay | 傳播延遲 |
| fan-out | 扇出 |
| fan-in | 扇入 |
| flip-flop | 正反器 |
| latch | 閂鎖 |
| memory cell | 記憶體單元 |
| static RAM | 靜態 RAM (SRAM) |
| dynamic RAM | 動態 RAM (DRAM) |
| sense amplifier | 感測放大器 |
| read | 讀取 |
| write | 寫入 |
| address decoder | 位址解碼器 |
