// Neural ODE-flavoured data for all My Day variants

const NODE_DATA = {
  user: { name: "Ferhat", focus_today: 1.4, focus_streak: 11 },
  date: { weekday: "Tuesday", short: "Tue · Mar 17", long: "Tuesday, March 17" },

  // Today's Pulse — 7 papers, ranked
  pulse: {
    generated_at: "06:14",
    next_run: "06:00",
    cards: [
      {
        id: 101, rank: 1, score: 0.94,
        title: "Stiff Neural ODEs via Implicit-Explicit Solvers with Adaptive Step Control",
        authors: ["L. Chen", "P. Kidger", "M. Rackauckas"],
        venue: "ICLR 2026", year: 2026, days: 2,
        tldr: "Closes the long-standing stability gap on stiff systems by interleaving implicit Rosenbrock steps with cheap explicit corrections. ~6.4× faster than Dopri8 on Robertson benchmarks at matched tolerance.",
        why: ["Cites Kidger 2022 (in your library, ★★★)", "Topic match: stiff solvers (1.0)", "Author overlap with Rackauckas thread"],
        breakdown: { embedding: 0.31, llm: 0.36, recency: 0.17, graph: 0.10 },
        tags: ["stiff", "solvers", "diffrax"],
        action: "primary"
      },
      {
        id: 102, rank: 2, score: 0.88,
        title: "Probability-Flow ODEs Beat SDE Sampling on Long-Horizon Generation",
        authors: ["Y. Song", "J. Ho", "C. Saharia"],
        venue: "NeurIPS 2025", year: 2025, days: 4,
        tldr: "Empirical study showing deterministic probability-flow ODEs match or exceed reverse-SDE sampling once horizon exceeds ~2k steps; attributes gain to score-error accumulation in stochastic paths.",
        why: ["You read 3 papers in score-based diffusion this month", "Cites Chen 2018 (your starred paper)"],
        breakdown: { embedding: 0.34, llm: 0.31, recency: 0.13, graph: 0.10 },
        tags: ["diffusion", "score-based", "sampling"]
      },
      {
        id: 103, rank: 3, score: 0.81,
        title: "Latent Neural CDEs for Irregularly-Sampled Multivariate Time Series",
        authors: ["P. Kidger", "J. Morrill", "C. Salvi", "T. Lyons"],
        venue: "JMLR 2025", year: 2025, days: 6,
        tldr: "Extends Neural CDEs to a latent-space formulation; signature features as control path. Beats GRU-D on MIMIC-III sepsis prediction by 4.1 AUC.",
        why: ["Author follow: Kidger", "Topic match: CDEs + signature methods"],
        breakdown: { embedding: 0.30, llm: 0.27, recency: 0.14, graph: 0.10 },
        tags: ["CDE", "signatures", "time-series"]
      },
      {
        id: 104, rank: 4, score: 0.76,
        title: "Symplectic Hamiltonian Neural Networks for Long-Term Energy Conservation",
        authors: ["S. Greydanus", "M. Cranmer"],
        venue: "arXiv 2025", year: 2025, days: 1,
        tldr: "Symplectic integrator wrapped around an HNN core; 90× lower energy drift on N-body over 10⁴ steps vs. baseline HNN.",
        why: ["Topic match: Hamiltonian NNs", "Recency boost (1d)"],
        breakdown: { embedding: 0.28, llm: 0.26, recency: 0.18, graph: 0.04 },
        tags: ["HNN", "symplectic", "physics"]
      },
      {
        id: 105, rank: 5, score: 0.71,
        title: "Continuous Normalizing Flows Without Trace Estimation",
        authors: ["W. Grathwohl", "R. Chen", "J. Bettencourt"],
        venue: "ICML 2025", year: 2025, days: 9,
        tldr: "Replaces Hutchinson trace estimator with a closed-form layer family; FFJORD-style models train 2.3× faster at matched NLL on tabular benchmarks.",
        why: ["Cites your Chen 2018 starred paper"],
        breakdown: { embedding: 0.26, llm: 0.24, recency: 0.11, graph: 0.10 },
        tags: ["FFJORD", "CNF", "density estimation"]
      },
      {
        id: 106, rank: 6, score: 0.68,
        title: "Adjoint Sensitivity Without Memory: Reversible Solvers in JAX",
        authors: ["J. Bradbury", "P. Kidger"],
        venue: "arXiv 2025", year: 2025, days: 3,
        tldr: "Reversible Heun and reversible RK4 solvers eliminate the O(T) memory cost of standard backprop-through-time on Neural ODEs; 18× longer trajectories at fixed VRAM.",
        why: ["You bookmarked diffrax twice this week"],
        breakdown: { embedding: 0.24, llm: 0.23, recency: 0.15, graph: 0.06 },
        tags: ["adjoint", "reversible", "jax"]
      },
      {
        id: 107, rank: 7, score: 0.62,
        title: "When Does a Neural ODE Need More Than One Hidden Layer? A Vector-Field Capacity Analysis",
        authors: ["E. Dupont", "A. Doucet", "Y. Teh"],
        venue: "ICLR 2026", year: 2026, days: 5,
        tldr: "Theoretical bound on representable vector fields as a function of width and depth; empirical evidence that single-layer Neural ODEs underfit on homeomorphism-breaking tasks.",
        why: ["Cites Augmented Neural ODEs (Dupont 2019, in library)"],
        breakdown: { embedding: 0.22, llm: 0.21, recency: 0.13, graph: 0.06 },
        tags: ["theory", "expressivity"]
      }
    ]
  },

  // Action items — papers ingested but not processed
  action_items: [
    { id: 201, title: "Neural Stochastic Differential Equations: Deep Latent Gaussian Models in the Diffusion Limit", state: "pdf-ready", from: "Telegram · 2h ago" },
    { id: 202, title: "Diffrax: Numerical Differential Equation Solvers in JAX", state: "pdf-ready", from: "GitHub watch · yesterday" },
    { id: 203, title: "Liquid Time-constant Networks", state: "no-pdf", from: "ArXiv RSS · 4h ago" },
  ],

  // Missing foundational — gaps the system detected
  missing_foundational: [
    { id: 301, title: "Neural Ordinary Differential Equations", authors: "Chen, Rubanova, Bettencourt, Duvenaud", year: 2018, citations: 4127, reason: "Cited by 6 papers in your reading queue" },
    { id: 302, title: "Augmented Neural ODEs", authors: "Dupont, Doucet, Teh", year: 2019, citations: 612, reason: "Cited by 4 papers you read this week" },
  ],

  // Tasks for today
  tasks: [
    { id: 1, title: "Reread §4 of Kidger 2022 on adjoint methods", project: "thesis-ch3", color: "#2563eb", priority: "high" },
    { id: 2, title: "Run stiff-solver benchmark on Robertson at tol=1e-8", project: "experiments", color: "#16a34a", priority: "med" },
    { id: 3, title: "Outline §3.2: vector field capacity argument", project: "thesis-ch3", color: "#2563eb", priority: "med" },
    { id: 4, title: "Reply to Patrick on shared diffrax fork", project: null, priority: "low" },
  ],
  completed_today: [
    { id: 5, title: "Skim FFJORD revisited (#105)", at: "08:42" },
    { id: 6, title: "Add Hutchinson trace notes to Anki", at: "09:15" },
  ],

  // Active threads — half-finished readings/derivations
  threads: [
    { id: 401, title: "Reading: Latent ODEs for irregular time series (Rubanova 2019)", progress: 0.62, last_at: "yesterday 18:40", anchor: "§4.1 ELBO derivation" },
    { id: 402, title: "Note: Why does symplectic structure matter for HNNs?", progress: 0.30, last_at: "Mar 14", anchor: "Stuck on commutator argument" },
    { id: 403, title: "Derivation: Adjoint backward pass with checkpointing", progress: 0.85, last_at: "this morning 09:02", anchor: "Final memory bound" },
  ],

  // Cards / FSRS
  cards: { due: 14, learning: 6, retention_30d: 0.87, streak: 11, reviewed_today: 5 },

  // Projects
  projects: [
    { id: 1, name: "Thesis Ch.3 — Stiff Neural ODEs", progress: 64, milestone: "First full draft", due: "Mar 28", status: "on-track" },
    { id: 2, name: "Diffrax stiff-solver PR", progress: 31, milestone: "Open draft PR", due: "Apr 02", status: "at-risk" },
    { id: 3, name: "Lit review: physics-informed", progress: 88, milestone: "Submit to RG", due: "Mar 19", status: "on-track" },
  ],

  // Yesterday's carryover (Ritual variant)
  yesterday: {
    focused: 2.3,
    cards_reviewed: 9,
    completed: ["Drafted §3 intro on flow homeomorphisms", "Reviewed 9 cards"],
    deferred: ["Run Robertson benchmark", "Reread Kidger §4"],
  },

  // EOD reflection prompt slots
  eod: {
    one_thing: "",
    blocker: "",
    tomorrow_first: "",
  },

  // Service health
  services: { paper_ingestion: "ok", learning_engine: "ok", pulse_runner: "ok", litellm: "ok", queue_depth: 2 },
};

window.NODE_DATA = NODE_DATA;
