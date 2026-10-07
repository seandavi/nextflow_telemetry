import { defineWorkersConfig } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersConfig({
  test: {
    poolOptions: {
      workers: {
        singleWorker: true,
        // The lifecycle suite is one narrative: each test picks up the corpus
        // the previous one left behind. Per-test storage rollback would undo it.
        isolatedStorage: false,
        wrangler: { configPath: "./wrangler.jsonc" },
      },
    },
  },
});
