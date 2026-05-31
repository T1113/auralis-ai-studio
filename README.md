# Auralis - AI Portrait Studio 📸

[English](#english) | [中文](#chinese)

---

<a id="english"></a>
## English

Auralis is a premium, open-source AI Portrait Studio. It allows users to generate professional ID photos and business portraits directly from their browser, eliminating the need to visit a physical photo studio. 

The project features a modern Glassmorphism frontend and integrates seamlessly with OpenAI's Image API (via a secure Cloudflare Worker backend) to process and deliver stunning AI-enhanced headshots.

### Features ✨
- **Modern UI/UX**: Built with Vanilla JS, CSS3, and Vite, featuring glassmorphism design and smooth processing animations.
- **AI Processing**: Integration with OpenAI's Image API to automatically process, crop, and generate high-quality portraits.
- **Customizable**: Choose between ID Photo (with dynamic background colors) or Professional business styles.
- **Cloudflare Worker Backend**: A scalable serverless backend configured to securely handle API keys and image processing tasks.

### Quick Start 🚀
1. Clone the repository.
2. Setup the Frontend:
   ```bash
   cd frontend
   npm install
   # Create a .env file and add VITE_OPENAI_API_KEY for local UI testing
   npm run dev
   ```
3. Setup the Backend:
   ```bash
   cd cloudflare-worker
   npm install
   npm run deploy
   ```

---

<a id="chinese"></a>
## 中文

Auralis 是一个极致优雅的开源 AI 证件照与形象照生成平台。用户可以直接在浏览器中生成专业的证件照和商务形象照，再也不用特意跑去照相馆。

本项目前端采用现代化的“玻璃拟物化”设计，后端基于 Cloudflare Worker 架构，安全且无缝地集成了 OpenAI 的图像生成 API，为用户提供惊艳的 AI 换图与修图体验。

### 核心特性 ✨
- **高颜值交互界面**：基于 Vite + 原生 JS 构建，包含极其流畅的拖拽上传与“魔法加载”动画。
- **强大的 AI 核心**：深度接入 OpenAI 图像处理能力，自动完成抠图、背景替换与人像美化。
- **多样化模式**：支持标准证件照（红/白/蓝底一键切换）以及高级商务形象照模式。
- **Serverless 架构**：后端使用 Cloudflare Worker，安全隔离敏感的 API Key，支持极速部署。

### 快速启动 🚀
1. 克隆项目。
2. 启动前端界面：
   ```bash
   cd frontend
   npm install
   # 可以在本地创建 .env 文件并填入 VITE_OPENAI_API_KEY 进行界面测试
   npm run dev
   ```
3. 部署后端 Worker：
   ```bash
   cd cloudflare-worker
   npm install
   npm run deploy
   ```

## License 📝
MIT License
