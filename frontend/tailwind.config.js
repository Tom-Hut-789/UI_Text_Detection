/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  // 关闭 preflight，避免 Tailwind 的全局重置覆盖 Element Plus 组件样式
  corePlugins: { preflight: false },
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#eef4ff',
          100: '#d9e5ff',
          500: '#3b6fd4',
          600: '#2f5cb8',
          700: '#254a94',
          900: '#16295a',
        },
      },
    },
  },
  plugins: [],
}
