import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL,
  timeout: 30000,
})

// Attach the JWT on every request. Wired up properly on Day 1;
// reading from localStorage is enough for now.
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('truetape_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// Normalise every error into one shape, so no component ever has to
// guess whether the failure was network, 4xx, or 5xx.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const normalised = {
      code: error.response?.data?.error?.code ?? 'NETWORK_ERROR',
      message:
        error.response?.data?.error?.message ??
        error.message ??
        'Something went wrong',
      status: error.response?.status ?? 0,
      details: error.response?.data?.error?.details ?? null,
    }
    return Promise.reject(normalised)
  },
)

export default api