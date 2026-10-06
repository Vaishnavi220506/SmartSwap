// SmartSwap - Review 1 & 2 deck (BCSE303P). Every number here comes from the study files.
const pptxgen = require("pptxgenjs");
const fs = require("fs");
const path = require("path");

const DIR = __dirname;
const data = JSON.parse(fs.readFileSync(path.join(DIR, "data.json"), "utf8"));
const img = (f) => path.join(DIR, f);

const INK = "14213D", BODY = "3A4256", MUTED = "6B7280", LINE = "D9DEE7";
const BLUE = "2A78D6", ORANGE = "E8590C", GREEN = "15925E", VIOLET = "4A3AA7";
const TINT = "F3F6FB", ORANGE_T = "FFF1EA", GREEN_T = "E8F6EF", BLUE_T = "EAF2FC", VIOLET_T = "EFEDFA";
const H = "Segoe UI Semibold", B = "Segoe UI";

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.333 x 7.5
pres.title = "SmartSwap - Project Reviews 1 & 2";
pres.author = "SmartSwap team";

pres.defineSlideMaster({
  title: "CONTENT", background: { color: "FFFFFF" },
  objects: [
    { text: { text: "SmartSwap  ·  BCSE303P Operating Systems Lab", options: { x: 0.6, y: 7.0, w: 6, h: 0.3, fontFace: B, fontSize: 10, color: MUTED, margin: 0 } } },
  ],
  slideNumber: { x: 12.3, y: 7.0, w: 0.5, h: 0.3, fontFace: B, fontSize: 10, color: MUTED, align: "right" },
});
pres.defineSlideMaster({ title: "DARK", background: { color: INK } });

function header(slide, kicker, review, title) {
  slide.addText(kicker, { x: 0.6, y: 0.42, w: 8, h: 0.3, fontFace: H, fontSize: 12, color: ORANGE, charSpacing: 2, margin: 0, isTextBox: true });
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 11.25, y: 0.38, w: 1.5, h: 0.36, rectRadius: 0.18, fill: { color: TINT }, line: { color: LINE, width: 0.75 } });
  slide.addText(review, { x: 11.25, y: 0.38, w: 1.5, h: 0.36, fontFace: H, fontSize: 11, color: INK, align: "center", valign: "middle", margin: 0, isTextBox: true });
  slide.addText(title, { x: 0.6, y: 0.82, w: 12.1, h: 1.05, fontFace: H, fontSize: 28, color: INK, valign: "top", margin: 0, isTextBox: true, fit: "none" });
}
const shadow = () => ({ type: "outer", color: "1B2A4A", opacity: 0.18, blur: 10, offset: 3, angle: 90 });

// ---------------------------------------------------------------- 1. Title
{
  const s = pres.addSlide({ masterName: "DARK" });
  s.addText("BCSE303P  ·  OS LAB  ·  PROJECT REVIEWS 1 & 2", { x: 0.7, y: 0.75, w: 6.2, h: 0.3, fontFace: H, fontSize: 11, color: "9FB3D9", charSpacing: 2, margin: 0, isTextBox: true });
  s.addText("SmartSwap", { x: 0.7, y: 1.35, w: 6.2, h: 1.0, fontFace: H, fontSize: 54, color: "FFFFFF", margin: 0, isTextBox: true });
  s.addText("Measuring, and fixing, the time background work steals from the app you are using on stock Windows", { x: 0.7, y: 2.45, w: 5.8, h: 1.3, fontFace: B, fontSize: 20, color: "CBD5E1", margin: 0, isTextBox: true, valign: "top" });
  const facts = [["No admin rights", "user-mode only"], ["Names the cause", "CPU · memory · storage"], ["Proves every fix", "read back from Windows"]];
  facts.forEach(([a, b], i) => {
    s.addText([{ text: a, options: { fontFace: H, fontSize: 14, color: "FFFFFF", breakLine: true } }, { text: b, options: { fontFace: B, fontSize: 11, color: "9FB3D9" } }],
      { x: 0.7 + i * 1.95, y: 4.2, w: 1.85, h: 0.75, margin: 0, isTextBox: true, valign: "top" });
  });
  s.addText("Team: Name One (Reg. No.)  ·  Name Two (Reg. No.)  ·  Name Three (Reg. No.)\nGuide: Prof. Name  ·  School of Computer Science and Engineering, VIT", { x: 0.7, y: 6.15, w: 6.3, h: 0.7, fontFace: B, fontSize: 12, color: "94A3B8", margin: 0, isTextBox: true, valign: "top" });
  s.addImage({ path: img("crop_monitor.png"), x: 7.05, y: 1.05, w: 5.75, h: 5.75 * 1320 / 1740, shadow: { type: "outer", color: "000000", opacity: 0.45, blur: 18, offset: 6, angle: 90 } });
  s.addText("The live dashboard during real CPU contention: 80% of the canary's time lost, cause named, cap stage reached.", { x: 7.05, y: 1.05 + 5.75 * 1320 / 1740 + 0.15, w: 5.75, h: 0.45, fontFace: B, fontSize: 10.5, color: "94A3B8", margin: 0, isTextBox: true });
  s.addNotes("Introduce the team and the one-line idea. SmartSwap answers a question Windows cannot answer today: how much time is background work stealing from the app I am using, which resource is responsible, and did the fix actually help. The screenshot is the real dashboard, captured while we injected CPU load.");
}

// ---------------------------------------------------------------- 2. Problem
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "01 · PROBLEM STATEMENT", "Review 1", "Task Manager shows how busy the PC is, not how much you are being slowed down");
  const pts = [
    ["Background work never stops.", "Sync, search indexing, updates and builds run while you work. On our test laptop they used 3–4 cores without being asked."],
    ["Busy is the wrong signal.", "Memory-bandwidth contention can make an app several times slower while half the cores are idle. No Task Manager counter shows it."],
    ["Today's fixes are blind.", "Priority tweaks and “optimizer” tools change settings without measuring whether the user actually benefits."],
  ];
  pts.forEach(([lead, text], i) => {
    const y = 2.1 + i * 1.12;
    s.addShape(pres.shapes.OVAL, { x: 0.6, y: y + 0.04, w: 0.42, h: 0.42, fill: { color: i === 2 ? ORANGE_T : BLUE_T }, line: { type: "none" } });
    s.addText(String(i + 1), { x: 0.6, y: y + 0.04, w: 0.42, h: 0.42, fontFace: H, fontSize: 14, color: i === 2 ? ORANGE : BLUE, align: "center", valign: "middle", margin: 0, isTextBox: true });
    s.addText([{ text: lead + " ", options: { fontFace: H, color: INK } }, { text, options: { fontFace: B, color: BODY } }], { x: 1.2, y, w: 5.6, h: 1.0, fontSize: 15, margin: 0, isTextBox: true, valign: "top" });
  });
  // evidence pair
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.3, y: 2.05, w: 5.45, h: 1.45, rectRadius: 0.12, fill: { color: BLUE_T }, line: { type: "none" } });
  s.addText([{ text: "8 of 16", options: { fontFace: H, fontSize: 40, color: BLUE, breakLine: true } }, { text: "CPU cores idle under memory-bandwidth load", options: { fontFace: B, fontSize: 14, color: BODY } }], { x: 7.6, y: 2.12, w: 5.0, h: 1.3, margin: 0, isTextBox: true, valign: "middle" });
  s.addText("yet", { x: 7.3, y: 3.55, w: 5.45, h: 0.35, fontFace: B, fontSize: 13, italic: true, color: MUTED, align: "center", margin: 0, isTextBox: true });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.3, y: 3.95, w: 5.45, h: 1.45, rectRadius: 0.12, fill: { color: ORANGE_T }, line: { type: "none" } });
  s.addText([{ text: "2.6× slower", options: { fontFace: H, fontSize: 40, color: ORANGE, breakLine: true } }, { text: "interactive tail latency (p95 3.95 → 10.8 ms)", options: { fontFace: B, fontSize: 14, color: BODY } }], { x: 7.6, y: 4.02, w: 5.0, h: 1.3, margin: 0, isTextBox: true, valign: "middle" });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.6, y: 5.7, w: 12.15, h: 1.05, rectRadius: 0.1, fill: { color: TINT }, line: { type: "none" } });
  s.addText([{ text: "Problem statement  ", options: { fontFace: H, color: INK } }, { text: "Unprivileged software on Windows cannot measure how much time interactive work loses to background interference, tell which resource causes it, or verify that a mitigation helped.", options: { fontFace: B, color: BODY } }],
    { x: 0.85, y: 5.7, w: 11.7, h: 1.05, fontSize: 15, margin: 0, isTextBox: true, valign: "middle" });
  s.addNotes("Problem statement (1 mark). Everyone has felt a laptop lag while OneDrive or Windows Update runs. Task Manager shows utilisation, which is not the same as harm. Our measured example: with memory-bandwidth load, half the cores are idle, yet the interactive task's tail latency is 2.6 times worse. Read the formal problem statement at the bottom.");
}

// ---------------------------------------------------------------- 3. Literature review
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "02 · LITERATURE REVIEW", "Review 1", "Prior work measures stalls or controls interference, but not on an unprivileged desktop");
  const hd = (t) => ({ text: t, options: { bold: true, color: "FFFFFF", fill: { color: INK }, fontFace: H } });
  const rows = [
    [hd("Work"), hd("What it contributes"), hd("Why it does not solve our problem")],
    ["Linux PSI (Weiner, 2018)", "Kernel-reported share of time tasks stall on CPU, memory or I/O", "Linux kernel only; Windows has no user-mode equivalent"],
    ["Heracles (ISCA 2015), PARTIES (ASPLOS 2019)", "Feedback controllers that co-locate latency-critical and batch jobs", "Need server cache/bandwidth partitioning and privileged control"],
    ["Caladan (OSDI 2020)", "Reacts to interference within microseconds", "Kernel-bypass runtime; the protected app must be rewritten"],
    ["Bubble-Up, Bubble-Flux (MICRO 2011, ISCA 2013)", "Synthetic probes measure how sensitive a job is to contention", "Offline profiling for data-centre placement"],
    ["Windows tools: Task Manager, priority, EcoQoS", "Show utilisation; static deprioritisation", "No harm measurement, no cause, no proof a change helped"],
  ];
  s.addTable(rows, { x: 0.6, y: 2.0, w: 12.15, colW: [3.35, 4.35, 4.45], fontFace: B, fontSize: 13, color: BODY, border: { type: "solid", pt: 0.75, color: LINE }, rowH: [0.42, 0.62, 0.62, 0.62, 0.62, 0.62], valign: "middle", margin: [0.06, 0.12, 0.06, 0.12] });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.6, y: 5.95, w: 12.15, h: 0.8, rectRadius: 0.1, fill: { color: ORANGE_T }, line: { type: "none" } });
  s.addText([{ text: "Research gap  ", options: { fontFace: H, color: ORANGE } }, { text: "No privilege-free way on Windows to measure interference harm, name its cause, and prove that a fix worked.", options: { fontFace: B, color: INK } }],
    { x: 0.85, y: 5.95, w: 11.7, h: 0.8, fontSize: 15, margin: 0, isTextBox: true, valign: "middle" });
  s.addNotes("Literature review (1 mark). Linux solved measurement with PSI in 2018. Data-centre systems like Heracles, PARTIES and Caladan control interference, but assume server hardware, root access, or modified applications. Bubble-Up inspired our canary idea. Windows tools show utilisation only. That leaves the gap in orange.");
}

// ---------------------------------------------------------------- 4. Objectives
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "03 · PROJECT OBJECTIVES", "Review 1", "Five measurable objectives, all met in a 460-trial evaluation");
  const obj = [
    ["Measure interference harm from user mode, without admin rights", "Stall index tracks the real app's p95: ρ = 0.96"],
    ["Identify which resource is contended", "Cause named correctly in 86–99.6% of stalled periods"],
    ["Apply the cheapest effective fix automatically", "Tail latency cut by up to 94%"],
    ["Verify every action and every revert", "1,575 / 1,575 actions · 322 / 322 reverts verified"],
    ["Evaluate rigorously, including the cost to background work", "Randomised blocks, exact Wilcoxon, Holm, bootstrap CIs"],
  ];
  obj.forEach(([o, a], i) => {
    const y = 2.05 + i * 0.93;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.6, y, w: 12.15, h: 0.8, rectRadius: 0.1, fill: { color: i % 2 ? "FFFFFF" : TINT }, line: { color: LINE, width: 0.75 } });
    s.addText(`O${i + 1}`, { x: 0.8, y, w: 0.7, h: 0.8, fontFace: H, fontSize: 18, color: BLUE, valign: "middle", margin: 0, isTextBox: true });
    s.addText(o, { x: 1.5, y, w: 5.9, h: 0.8, fontFace: B, fontSize: 15, color: INK, valign: "middle", margin: 0, isTextBox: true });
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 7.55, y: y + 0.14, w: 5.0, h: 0.52, rectRadius: 0.26, fill: { color: GREEN_T }, line: { type: "none" } });
    s.addText([{ text: "✓  ", options: { fontFace: H, color: GREEN } }, { text: a, options: { fontFace: B, color: "0F5132" } }], { x: 7.75, y: y + 0.14, w: 4.75, h: 0.52, fontSize: 13, valign: "middle", margin: 0, isTextBox: true });
  });
  s.addNotes("Project objectives (1 mark). Each objective has a measurable target and we show the measured result next to it. 460 trials = 340 in the main study plus 120 in the hybrid-core study, with zero failed trials.");
}

// ---------------------------------------------------------------- 5. Novelty
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "04 · NOVELTY & INNOVATION", "Review 1", "Four ideas that no Windows tool combines today");
  const cards = [
    ["1", "Windows Stall Index", "A tiny canary times its own delays to estimate how much time interactive work is losing: a PSI-style signal Windows lacks.", "ρ = 0.96 with the real app's p95 · 1.2% false alarms", BLUE, BLUE_T],
    ["2", "Specificity-ordered attribution", "Checks the most specific symptom first: CPU, then memory, then storage. The naive “biggest slowdown” rule blames storage for memory contention.", "Cause correct in 86–99.6% of stalled periods", VIOLET, VIOLET_T],
    ["3", "Least-cost, double-verified ladder", "Priority first, CPU cap only if needed. Every action is read back from Windows and kept only if the stall really drops.", "Without the ladder: 4.8× worse p95", GREEN, GREEN_T],
    ["4", "Hybrid-core steering", "On P-core/E-core laptops, moves background work onto efficiency cores before throttling it.", "Memory contention: 27% lower p95 than the best static policy", ORANGE, ORANGE_T],
  ];
  cards.forEach(([n, t, d, e, c, tint], i) => {
    const x = 0.6 + (i % 2) * 6.17, y = 2.0 + Math.floor(i / 2) * 2.45;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 5.98, h: 2.25, rectRadius: 0.12, fill: { color: "FFFFFF" }, line: { color: LINE, width: 0.75 }, shadow: shadow() });
    s.addShape(pres.shapes.OVAL, { x: x + 0.3, y: y + 0.28, w: 0.5, h: 0.5, fill: { color: tint }, line: { type: "none" } });
    s.addText(n, { x: x + 0.3, y: y + 0.28, w: 0.5, h: 0.5, fontFace: H, fontSize: 16, color: c, align: "center", valign: "middle", margin: 0, isTextBox: true });
    s.addText(t, { x: x + 0.95, y: y + 0.26, w: 4.8, h: 0.55, fontFace: H, fontSize: 18, color: INK, valign: "middle", margin: 0, isTextBox: true });
    s.addText(d, { x: x + 0.3, y: y + 0.88, w: 5.45, h: 0.85, fontFace: B, fontSize: 13, color: BODY, valign: "top", margin: 0, isTextBox: true });
    s.addText(e, { x: x + 0.3, y: y + 1.72, w: 5.45, h: 0.38, fontFace: H, fontSize: 12.5, color: c, valign: "middle", margin: 0, isTextBox: true });
  });
  s.addNotes("Novelty (3 marks), the most important slide. One: Windows has no user-mode stall metric like Linux PSI, so we built one and validated it against a separate app. Two: attribution ordered by how specific each symptom is; the naive rule gets memory contention wrong. Three: cheapest fix first, every action verified twice, and the ablation shows the ladder matters. Four: on hybrid CPUs we steer background work to E-cores before throttling, which beat static priority on memory contention with a 95% CI of 0.67 to 0.75.");
}

// ---------------------------------------------------------------- 6. Architecture
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "05 · SYSTEM DESIGN & ARCHITECTURE", "Review 2", "A closed control loop that runs every 200 ms, entirely in user mode");
  s.addImage({ path: img("fig_architecture.png"), x: 0.6, y: 1.95, w: 12.15, h: 12.15 * 615 / 2100 });
  const cols = [
    ["Sense", "Canary wakes every 40 ms and times scheduling delay, compute, an 8 MiB memory copy and a 4 KiB unbuffered read.", BLUE],
    ["Decide", "WSI = share of time lost. Cause = first symptom above 2× idle. Ladder level chosen against a 3% target.", VIOLET],
    ["Act & prove", "Priority, I/O priority, E-core affinity or Job CPU cap via Win32/NT; read back, check the benefit, revert when quiet.", GREEN],
  ];
  cols.forEach(([t, d, c], i) => {
    const x = 0.6 + i * 4.1;
    s.addText([{ text: t, options: { fontFace: H, fontSize: 16, color: c, breakLine: true } }, { text: d, options: { fontFace: B, fontSize: 13, color: BODY } }], { x, y: 5.75, w: 3.85, h: 1.15, margin: 0, isTextBox: true, valign: "top" });
  });
  s.addNotes("System design and architecture (1 mark). Walk left to right: the canary feels the contention, the WSI turns it into a percentage of time lost and names the cause, the controller picks the cheapest action, the actuator applies it through documented Windows APIs and reads it back. The dashed line: SmartSwap only releases a fix once the background group is actually quiet. The foreground app is never modified.");
}

// ---------------------------------------------------------------- 7. Modules
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "06 · MODULE IDENTIFICATION", "Review 2", "Six modules, each testable on its own");
  const mods = [
    ["M1", "Canary & probes", "probes.py", "40 ms canary, foreground probe, CPU / memory / storage workloads"],
    ["M2", "WSI & attribution", "controller.py", "Stall index and specificity-ordered cause detection"],
    ["M3", "Control ladder", "controller.py", "Gating, hint → E-core steer → AIMD cap, benefit check, release"],
    ["M4", "Verified actuator", "native.py", "Priority, I/O priority, affinity and Job CPU cap with read-back"],
    ["M5", "Experiment engine", "engine.py · run_study.py", "Trials, randomised blocks, background-throughput accounting"],
    ["M6", "Analysis & dashboard", "analyze.py · app.py · frontend", "Statistics, figures, live web UI and exports"],
  ];
  mods.forEach(([id, name, file, what], i) => {
    const x = 0.6 + (i % 3) * 4.1, y = 2.0 + Math.floor(i / 3) * 2.4;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 3.95, h: 2.2, rectRadius: 0.12, fill: { color: TINT }, line: { type: "none" } });
    s.addText(id, { x: x + 0.3, y: y + 0.25, w: 0.8, h: 0.4, fontFace: H, fontSize: 15, color: BLUE, margin: 0, isTextBox: true });
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: x + 2.45, y: y + 0.25, w: 1.25, h: 0.36, rectRadius: 0.18, fill: { color: GREEN_T }, line: { type: "none" } });
    s.addText("✓ Complete", { x: x + 2.45, y: y + 0.25, w: 1.25, h: 0.36, fontFace: H, fontSize: 10.5, color: GREEN, align: "center", valign: "middle", margin: 0, isTextBox: true });
    s.addText(name, { x: x + 0.3, y: y + 0.7, w: 3.4, h: 0.45, fontFace: H, fontSize: 17, color: INK, margin: 0, isTextBox: true });
    s.addText(file, { x: x + 0.3, y: y + 1.12, w: 3.4, h: 0.3, fontFace: "Consolas", fontSize: 11, color: MUTED, margin: 0, isTextBox: true });
    s.addText(what, { x: x + 0.3, y: y + 1.45, w: 3.4, h: 0.65, fontFace: B, fontSize: 12.5, color: BODY, margin: 0, isTextBox: true, valign: "top" });
  });
  s.addNotes("Module identification (1 mark). Six modules with clear boundaries: probes, measurement and attribution, the control ladder, the Windows actuator, the experiment engine, and analysis plus dashboard. The controller is pure logic with no Windows calls, so it is unit-tested and can be replayed offline.");
}

// ---------------------------------------------------------------- 8. Module completion
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "07 · MODULE COMPLETION", "Review 2", "All six modules are implemented, tested and verified on real hardware");
  const iw = 6.6, ih = iw * 1120 / 1740;
  s.addImage({ path: img("crop_trial.png"), x: 0.6, y: 1.95, w: iw, h: ih, shadow: shadow() });
  s.addText("Live trial from the dashboard: memory-bandwidth contention, p95 5.49 ms, 24 / 24 actions verified while AIMD tunes the cap.", { x: 0.6, y: 1.95 + ih + 0.12, w: iw, h: 0.5, fontFace: B, fontSize: 11, color: MUTED, margin: 0, isTextBox: true });
  const stats = [["6 / 6", "modules complete", BLUE, BLUE_T], ["25", "automated tests passing", VIOLET, VIOLET_T], ["460", "trials run, 0 failed", ORANGE, ORANGE_T], ["1,575 / 1,575", "actions verified by read-back", GREEN, GREEN_T]];
  stats.forEach(([v, l, c, tint], i) => {
    const x = 7.6 + (i % 2) * 2.62, y = 1.95 + Math.floor(i / 2) * 2.0;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: 2.5, h: 1.8, rectRadius: 0.12, fill: { color: tint }, line: { type: "none" } });
    s.addText([{ text: v, options: { fontFace: H, fontSize: v.length > 6 ? 24 : 32, color: c, breakLine: true } }, { text: l, options: { fontFace: B, fontSize: 12.5, color: BODY } }], { x: x + 0.2, y, w: 2.15, h: 1.8, margin: 0, isTextBox: true, valign: "middle" });
  });
  s.addText("Also complete: live monitor page, CSV / JSON / HTML / PDF exports, emergency stop, and an IEEE-format paper draft.", { x: 7.6, y: 5.95, w: 5.15, h: 0.7, fontFace: B, fontSize: 12.5, color: BODY, margin: 0, isTextBox: true, valign: "top" });
  s.addNotes("Module completion (4 marks). This screenshot is a real trial from our dashboard: the controller lowered priority, then capped the background CPU rate and tuned it up and down with AIMD; every one of the 24 actions has a green tick because Windows confirmed it. 25 automated tests pass (23 backend, 2 frontend). We ran 460 controlled trials with zero failures.");
}

// ---------------------------------------------------------------- 9. Results: validity
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "08 · RESULTS", "Review 2", "The stall index tracks what the user actually feels");
  const order = [["idle", "No interference", "8A8984"], ["io", "Storage", "1BAF7A"], ["membw", "Memory bandwidth", "EB6834"], ["mixed", "Mixed", "4A3AA7"], ["cpu", "CPU", "2A78D6"]];
  const xs = data.scatter.map((p) => p.wsi);
  const series = [{ name: "X", values: xs }].concat(order.map(([k, label]) => ({ name: label, values: data.scatter.map((p) => (p.preset === k ? p.p95 : null)) })));
  s.addChart(pres.charts.SCATTER, series, {
    x: 0.6, y: 1.9, w: 7.2, h: 4.9, lineSize: 0, lineDataSymbol: "circle", lineDataSymbolSize: 9, chartColors: order.map((o) => o[2]),
    valAxisLogScaleBase: 10, catAxisLogScaleBase: 10, valAxisMinVal: 1, valAxisMaxVal: 300, catAxisMinVal: 0.1, catAxisMaxVal: 100,
    showValAxisTitle: true, valAxisTitle: "Real app's p95 latency (ms, log)", showCatAxisTitle: true, catAxisTitle: "Canary stall index, WSI (%, log)",
    valAxisTitleFontSize: 12, catAxisTitleFontSize: 12, valAxisTitleColor: BODY, catAxisTitleColor: BODY, valAxisTitleFontFace: B, catAxisTitleFontFace: B,
    valAxisLabelFontSize: 11, catAxisLabelFontSize: 11, valAxisLabelColor: MUTED, catAxisLabelColor: MUTED, valAxisLabelFontFace: B, catAxisLabelFontFace: B,
    valGridLine: { color: "E5E7EB", size: 0.75 }, catGridLine: { style: "none" }, showLegend: true, legendPos: "b", legendFontSize: 11, legendFontFace: B, legendColor: BODY,
  });
  const stats = [["ρ = 0.96", "rank correlation between the canary's WSI and the separate app's p95 (50 trials)", BLUE], ["AUC 0.965–1.000", "detecting interference, with 1.2% false alarms on a quiet PC", VIOLET], ["92 · 86 · 99.6%", "cause named correctly for CPU · memory · storage", GREEN]];
  stats.forEach(([v, l, c], i) => {
    const y = 1.95 + i * 1.6;
    s.addText([{ text: v, options: { fontFace: H, fontSize: 30, color: c, breakLine: true } }, { text: l, options: { fontFace: B, fontSize: 13.5, color: BODY } }], { x: 8.3, y, w: 4.45, h: 1.45, margin: 0, isTextBox: true, valign: "top" });
  });
  s.addNotes("Results 1: the key validation. In observe-only trials SmartSwap measures but never acts, so this is a clean test: the canary's stall index rises in lock-step with the real foreground's tail latency, rank correlation 0.96. Used as a detector it almost never fires on a quiet machine (1.2%) and it names the right cause most of the time.");
}

// ---------------------------------------------------------------- 10. Results: mitigation + next steps
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  header(s, "09 · RESULTS & NEXT STEPS", "Review 2 → 3", "Up to 94% lower tail latency, and E-core steering beats static priority on memory contention");
  s.addChart(pres.charts.BAR, [
    { name: "Static priority (baseline)", labels: ["CPU", "Memory bandwidth", "Mixed"], values: data.bars.nice },
    { name: "SmartSwap v2", labels: ["CPU", "Memory bandwidth", "Mixed"], values: data.bars.adaptive },
    { name: "v2 + E-core steering", labels: ["CPU", "Memory bandwidth", "Mixed"], values: data.bars.adaptive_hybrid },
  ], {
    x: 0.6, y: 2.0, w: 7.4, h: 4.75, barDir: "col", barGapWidthPct: 60, chartColors: ["9AA5B8", "2A78D6", "E8590C"],
    showValue: true, dataLabelPosition: "outEnd", dataLabelFontSize: 11, dataLabelFontFace: B, dataLabelColor: BODY, dataLabelFormatCode: "0\"%\"",
    valAxisMinVal: 0, valAxisMaxVal: 100, valAxisLabelFormatCode: "0\"%\"", showValAxisTitle: true, valAxisTitle: "p95 reduction vs. no mitigation", valAxisTitleFontSize: 12, valAxisTitleColor: BODY, valAxisTitleFontFace: B,
    valAxisLabelFontSize: 11, catAxisLabelFontSize: 12, valAxisLabelColor: MUTED, catAxisLabelColor: BODY, valAxisLabelFontFace: B, catAxisLabelFontFace: B,
    valGridLine: { color: "E5E7EB", size: 0.75 }, catGridLine: { style: "none" }, showLegend: true, legendPos: "b", legendFontSize: 11, legendFontFace: B, legendColor: BODY,
  });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 8.35, y: 2.0, w: 4.4, h: 2.15, rectRadius: 0.12, fill: { color: TINT }, line: { type: "none" } });
  s.addText([
    { text: "What we report honestly", options: { fontFace: H, fontSize: 14, color: INK, breakLine: true } },
    { text: "Static priority is a strong baseline; v2 matches it on CPU and mixed load.", options: { fontFace: B, fontSize: 12.5, color: BODY, bullet: true, breakLine: true } },
    { text: "Under memory contention, v2 without steering over-throttles: 56% of background work kept vs 92% for priority.", options: { fontFace: B, fontSize: 12.5, color: BODY, bullet: true, breakLine: true } },
    { text: "Steering is measured on one hybrid laptop (8 blocks); replication is next.", options: { fontFace: B, fontSize: 12.5, color: BODY, bullet: true } },
  ], { x: 8.6, y: 2.12, w: 4.0, h: 1.95, margin: 0, isTextBox: true, valign: "top", paraSpaceAfter: 4 });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 8.35, y: 4.35, w: 4.4, h: 2.4, rectRadius: 0.12, fill: { color: INK }, line: { type: "none" } });
  s.addText([
    { text: "Review 3 plan", options: { fontFace: H, fontSize: 14, color: "FFFFFF", breakLine: true } },
    { text: "IEEE-format paper drafted (8 pages)", options: { fontFace: B, fontSize: 12.5, color: "CBD5E1", bullet: true, breakLine: true } },
    { text: "Replicate on a second Windows laptop", options: { fontFace: B, fontSize: 12.5, color: "CBD5E1", bullet: true, breakLine: true } },
    { text: "Add a real app (browser frame time)", options: { fontFace: B, fontSize: 12.5, color: "CBD5E1", bullet: true, breakLine: true } },
    { text: "Submit to an IEEE conference", options: { fontFace: B, fontSize: 12.5, color: "CBD5E1", bullet: true } },
  ], { x: 8.6, y: 4.47, w: 4.0, h: 2.2, margin: 0, isTextBox: true, valign: "top", paraSpaceAfter: 4 });
  s.addNotes("Results 2 and next steps. From the 120-trial hybrid study: SmartSwap and static priority both remove over 90% of tail latency under CPU and mixed contention. Under memory-bandwidth contention, adding E-core steering gives the best result, 27% lower p95 than static priority. We say clearly where we do not win. For Review 3: the IEEE paper is drafted; we will replicate on a second laptop, add a real application, and submit.");
}

pres.writeFile({ fileName: path.join(DIR, "SmartSwap_Review_1_and_2.pptx") }).then((f) => console.log("wrote", f));
