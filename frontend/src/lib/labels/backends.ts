export const BACKEND_LABELS: Record<'vllm' | 'ollama', string> = {
  vllm: 'vLLM (high-throughput)',
  ollama: 'Ollama (default)',
};

export const BACKEND_TOOLTIP =
  'Ollama is the default local runtime. vLLM is optional and only applies when you already run it behind the local LiteLLM route.';
