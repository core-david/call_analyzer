// Runs `fn` over `items` with at most `limit` in flight. Each item is
// independent — one rejection is logged and the rest continue.
export async function runPool<T>(items: T[], limit: number, fn: (t: T) => Promise<void>) {
  const queue = [...items];
  const workers = Array.from({ length: Math.min(limit, queue.length) }, async () => {
    while (queue.length) {
      const item = queue.shift()!;
      try { await fn(item); } catch (e) { console.error(e); }
    }
  });
  await Promise.all(workers);
}
