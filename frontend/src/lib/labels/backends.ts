export const BACKEND_LABELS: Record<'vllm' | 'ollama', string> = {
  vllm: 'vLLM (high-throughput)',
  ollama: 'Ollama (default)',
};

export const BACKEND_TOOLTIP =
  'Both run locally on your hardware and use your GPU. Ollama is the default and can fall back to CPU; vLLM is an advanced, GPU-only option with higher throughput.';
