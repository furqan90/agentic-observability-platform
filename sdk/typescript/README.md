#  Observability SDK — TypeScript / Node.js

> **Status: Planned — not yet implemented.**

This SDK will provide the same facade for TypeScript/Node agents as the Python SDK
provides for Python agents. It will implement the full [CONTRACT.md](../CONTRACT.md).

## Planned public API

```typescript
import {
  getTracer, shutdown,
  traceAgent, traceTool,
  recordUsage,
  getLangchainHandler,   // for LangChain.js / LangGraph.js
  getOpenaiClient,       // drop-in openai wrapper
  getAnthropicClient,    // via @arizeai/openinference-instrumentation-anthropic
  getPropagationHeaders,
  restorePropagationContext,
} from '@platform/observability-sdk';

await getTracer();

const run = traceAgent({ name: 'my-agent', tags: ['demo'] })(
  async (userInput: string): Promise<string> => {
    const result = await search(userInput);
    recordUsage('my-agent', 'gpt-4o', 800, 200, 0.012);
    return result;
  }
);
```

## Dependencies (anticipated)

```json
{
  "langfuse": "^3.0.0",
  "@opentelemetry/sdk-node": "^0.50.0",
  "@opentelemetry/exporter-trace-otlp-http": "^0.50.0"
}
```

## Contributing

Before implementing, read [../CONTRACT.md](../CONTRACT.md) and complete the checklist in §8.
Package should be published as `@platform/observability-sdk` on npm (or a private registry).
