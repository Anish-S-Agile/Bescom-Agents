// Backend base URL. Default is empty = same origin: the Vite dev server proxies /api to the backend
// (vite.config.ts) and in production the API serves the UI. Set VITE_API_URL only for a split deployment.
export const API_URL: string = ((import.meta.env.VITE_API_URL as string | undefined) ?? '').replace(/\/+$/, '');

export const TOKEN_KEY = 'token';
export const USER_KEY = 'user';
