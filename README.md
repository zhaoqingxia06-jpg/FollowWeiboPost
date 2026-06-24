# FollowWeiboPost

> 面向消费电子品牌团队的轻量级微博竞品情报工具

[![Python](https://img.shields.io/badge/Python-3.8+-blue)](https://www.python.org)
[![Flask](https://img.shields.io/badge/Flask-2.x-lightgrey)](https://flask.palletsprojects.com)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## 简介

**FollowWeiboPost** 是一款运行在本地的微博竞品监控 Web 应用。  
输入竞品品牌的微博用户 ID，即可自动抓取其原创帖子，并通过大语言模型（智谱 GLM）提取结构化竞品动态——新品预热、参数曝光、促销活动、品牌宣传等——让团队无需手动刷微博，即可在一张竞品对比表中，纵览各品牌的内容节奏与产品动向。

---

## 功能特性

| 功能 | 说明 |
|------|------|
| 🔍 多账号监控 | 支持添加任意数量的微博用户 ID，侧边栏统一管理 |
| 📊 帖子列表 | 按时间范围或数量抓取，展示内容、图片、互动指标 |
| 🤖 AI 摘要 | 调用智谱 GLM API 提取事件类型/产品/卖点/一句话摘要 |
| 🖼️ 图片预览 | 缩略图展示 + 点击全屏预览，支持左右切换 |
| 📅 竞品对比 | 日期×品牌矩阵，近10天发帖频率图表，支持列拖拽排序 |
| ⚡ 增量抓取 | 已抓取帖子自动去重，新增帖子高亮标注 |
| 💾 本地缓存 | 帖子和摘要持久化到 JSON，重启不丢失（摘要7天TTL）|

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置文件

复制 `weibo_spider/config_sample.json` 为项目根目录下的 `config.json`，填入以下字段：

```json
{
    "cookie": "your_weibo_cookie_here",
    "llm_api_key": "your_zhipu_api_key_here",
    "llm_base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "llm_model": "glm-4-flash"
}
```

- **cookie**：登录 [weibo.com](https://weibo.com) 后从浏览器 DevTools → Application → Cookies 中复制
- **llm_api_key**：前往 [智谱 AI 开放平台](https://open.bigmodel.cn) 注册获取（免费额度）
- 不配置 `llm_api_key` 也可运行，摘要将降级为本地规则引擎

### 3. 启动服务

```bash
python web_app/server.py
```

浏览器访问 **http://localhost:5000**

---

## 使用说明

### 添加监控对象

在左侧「监控对象」区域输入微博用户数字 ID，点击 ＋ 添加。  
用户 ID 可在微博个人主页 URL 中找到：`weibo.com/<用户ID>`

### 抓取帖子

- **按数量**：输入抓取条数，点击「获取帖子」
- **按时间**：切换为「按时间」，选择起止日期，点击「获取帖子」

### 竞品对比

切换到「竞品对比」标签页，系统自动按日期×品牌聚合所有已抓取帖子，展示：
- 各品牌发帖频率柱状图（近10天）
- 每日每品牌帖子摘要卡片（可展开查看详细卖点）
- 支持列拖拽调整品牌显示顺序

---

## 项目结构

```
├── web_app/
│   ├── server.py          # Flask 后端（API + 图片代理 + 缓存）
│   └── static/
│       └── index.html     # 单页前端应用
├── weibo_spider/          # 微博抓取引擎（解析器）
├── config.json            # 本地配置（不纳入版本控制）
└── requirements.txt
```

---

## 技术栈

- **后端**：Python 3 + Flask
- **前端**：纯 HTML/CSS/JS（无框架依赖）
- **AI**：智谱 GLM-4-Flash（降级：本地规则引擎）
- **数据**：JSON 文件持久化（无需数据库）

---

## 注意事项

- 本工具仅供学习与内部竞品研究使用，请遵守微博平台使用协议
- `config.json` 包含个人 cookie，已加入 `.gitignore`，请勿上传
- Cookie 有效期通常为数天至数周，失效后需重新获取
- 抓取频率建议不要过高，避免触发平台限流

---

## License

MIT
