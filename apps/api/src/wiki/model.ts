/** The language model behind wikis: an interface (tests inject a fake) and the Anthropic client. */
import Anthropic from '@anthropic-ai/sdk';

export interface WikiPrompt {
  system: string;
  user: string;
}

export interface WikiCompletion {
  text: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  stop_reason: string | null;
}

export interface WikiModel {
  /** Model id recorded with each article (WIKI_MODEL). */
  readonly id: string;
  complete(prompt: WikiPrompt): Promise<WikiCompletion>;
}

export const DEFAULT_WIKI_MODEL = 'claude-haiku-5-5';
const MAX_TOKENS = 2000;

/** Official SDK client. The key is read once here and never logged or returned. */
export function anthropicModel(opts: { apiKey: string; model?: string; timeoutMs?: number }): WikiModel {
  const client = new Anthropic({ apiKey: opts.apiKey, maxRetries: 2, timeout: opts.timeoutMs ?? 90_000 });
  const id = opts.model || DEFAULT_WIKI_MODEL;
  return {
    id,
    async complete(prompt) {
      const msg = await client.messages.create({
        model: id,
        max_tokens: MAX_TOKENS,
        system: prompt.system,
        messages: [{ role: 'user', content: prompt.user }],
      });
      const text = msg.content
        .flatMap((b) => (b.type === 'text' ? [b.text] : []))
        .join('')
        .trim();
      return {
        text,
        model: msg.model,
        input_tokens: msg.usage.input_tokens,
        output_tokens: msg.usage.output_tokens,
        stop_reason: msg.stop_reason,
      };
    },
  };
}

export interface WikiSettings {
  /** null = generation disabled (no ANTHROPIC_API_KEY): POST answers 503. */
  model: WikiModel | null;
  /** WIKI_PUBLIC=true lets anonymous viewers generate. */
  public: boolean;
  /** Generations allowed per window, across all callers of this process. */
  rateLimit: { max: number; windowMs: number };
}

/** CONTRACTS §9: ANTHROPIC_API_KEY, WIKI_MODEL, WIKI_PUBLIC. */
export function wikiSettingsFromEnv(env: NodeJS.ProcessEnv = process.env): WikiSettings {
  const key = env.ANTHROPIC_API_KEY?.trim();
  return {
    model: key ? anthropicModel({ apiKey: key, model: env.WIKI_MODEL?.trim() || DEFAULT_WIKI_MODEL }) : null,
    public: env.WIKI_PUBLIC === 'true',
    rateLimit: { max: Number(env.WIKI_RATE_PER_MIN ?? 6) || 6, windowMs: 60_000 },
  };
}
