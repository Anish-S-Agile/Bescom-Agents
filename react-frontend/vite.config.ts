import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Build output: dist/ (served by the FastAPI app in production, see Dockerfile).
// The browser talks to its own origin and Vite forwards /api to the backend, so there is no CORS to configure
// and it does not matter which port Vite ends up on. 127.0.0.1 (not "localhost") avoids IPv6 resolution
// pointing at a server that only listens on IPv4. Change the target here if the API is not on port 8000.
const apiProxy = {
  '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: apiProxy },
  preview: { port: 4173, proxy: apiProxy },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
