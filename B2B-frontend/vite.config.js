import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
const backendTarget = 'http://127.0.0.1:8000';
export default defineConfig({
    plugins: [react()],
    server: {
        host: '127.0.0.1',
        port: 5173,
        strictPort: true,
        proxy: {
            '/api': { target: backendTarget, changeOrigin: true },
            '/health': { target: backendTarget, changeOrigin: true },
            '/version': { target: backendTarget, changeOrigin: true },
            '/mcp': { target: backendTarget, changeOrigin: true, ws: true },
        },
    },
});
