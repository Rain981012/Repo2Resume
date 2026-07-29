# Rain

wsy6476850901@gmail.com · [GitHub](https://github.com/Rain981012/Repo2Resume)

> 求职意向：Python 后端开发工程师

## 技能

- **主力方向**：Python 后端开发
- **语言**：Python (61%)、Jupyter (18%)、TypeScript (10%)、Vue (5%)、JavaScript (3%)、Shell (1%)
- **技术栈**：Python、TypeScript、Jupyter、Vue、JavaScript、Shell、SQL、FastAPI、Django、Django REST Framework、Vue3、React

## 项目经历

### 分布式社交平台后端

分布式社交平台后端，支持作者、帖子、inbox等API与跨节点通信

- **用户认证**：在分布式社交平台中，负责设计并实现安全的用户认证系统。采用CSRF token与JWT双重保障机制，构建了完整的用户注册、登录与身份验证流程，设计RESTful API接口确保系统安全性，有效保护用户数据安全。 <!-- src: socialdistribution, 实现用户认证模块（signup/login），使用CSRF token + JWT -->
- **消息收件箱**：面对分布式环境下的数据一致性挑战，开发inbox模块的完整CRUD功能。实现了帖子的获取、创建和删除操作，并设计跨节点通信机制，确保分布式环境下的数据一致性，提升了系统的可靠性和用户体验。 <!-- src: socialdistribution, 实现inbox模块的CRUD功能，支持post的获取、创建和删除 -->
- **API文档**：为提高系统可维护性，集成Swagger API文档系统为所有接口提供详细说明。同时编写signup/login功能的单元测试，确保认证模块的稳定性和可靠性，为后续开发和维护提供了有力支持。 <!-- src: socialdistribution, 添加Swagger API文档并编写signup/login单元测试 -->

### Git仓库分析简历生成工具

CLI agent：分析本地git仓库→技能画像→匹配职位→生成定制Markdown简历

- **核心开发**：为解决简历定制效率问题，独立开发CLI agent工具。通过分析本地git仓库自动生成技能画像与定制Markdown简历，实现命令行交互界面支持多参数配置与输出格式选择，显著提高了简历定制效率。 <!-- src: Repo2Resume, 独立开发CLI agent工具，分析本地git仓库生成技能画像与定制Markdown简历 -->
- **向量检索**：为实现精准技能匹配，使用chromadb+sentence-transformers构建向量检索系统。集成pydriller分析git提交历史，提取技术栈与项目经验信息，实现了技能语义匹配功能，提高了简历生成的准确性。 <!-- src: Repo2Resume, 使用chromadb + sentence-transformers构建向量检索，pydriller分析git提交历史 -->
- **LLM集成**：为增强智能内容创作能力，集成litellm调用LLM(Zhipu GLM)自动生成PR描述。设计确定性降级方案确保API不可用时的功能可用性，保证了系统在各种情况下都能稳定运行。 <!-- src: Repo2Resume, 集成litellm调用LLM (Zhipu GLM) 自动生成PR描述，含确定性降级方案 -->
- **CI/CD**：为确保代码质量和开发效率，配置GitHub Actions CI流水线实现自动化测试与部署。使用ruff进行代码规范检查与自动格式化，严格遵循PEP8标准，建立了完整的CI/CD流程。 <!-- src: Repo2Resume, 配置GitHub Actions CI流水线，使用ruff进行代码规范检查与自动格式化 -->

### AI末日星座生存游戏

AI末日星座生存游戏，Vue3前端+FastAPI后端+DeepSeek API

- **后端服务**：为支持AI驱动的生存游戏，开发FastAPI后端服务实现游戏状态管理与数据持久化。集成DeepSeek API提供AI对话功能，增强游戏交互体验，确保游戏数据安全和状态一致性，为玩家提供沉浸式游戏体验。 <!-- src: NLP_GAME, AI 末日星座生存游戏，Vue3 前端 + FastAPI 后端 + DeepSeek API -->
- **前端重构**：为提升游戏性能和用户体验，重构冒险游戏前端界面采用Vue3框架。优化组件结构与状态管理，改善用户交互体验，显著提升前端性能和响应速度，使游戏更加流畅和易用。 <!-- src: NLP_GAME, 重构冒险游戏前端（Vue3） -->

### PDF学术阅读与评审平台

面向课程场景的PDF学术阅读与评审平台，支持学生提交、教师反馈与评论协作

- **功能优化**：为提升平台安全性和用户体验，添加用户登录页面实现身份验证与权限控制。重构URL上传功能优化文件处理流程与加载性能，显著提升了平台安全性和文件处理效率。 <!-- src: groupproject-team_1, 添加登录页面，重构URL上传功能并优化加载流程 -->
- **讨论系统**：为促进师生间的学术交流，开发多级回复功能实现评论的正确缩进展示与层级管理。设计讨论数据结构支持师生间的学术交流与反馈，构建了完整的学术讨论环境。 <!-- src: groupproject-team_1, 添加讨论功能，实现多级回复与正确缩进展示 -->
- **问题修复**：为解决平台技术问题，修复图片无法打开的技术问题优化媒体文件处理逻辑。实现帖子删除功能支持内容管理与清理操作，提升了平台的内容管理能力和用户体验。 <!-- src: groupproject-team_1, 修复图片无法打开问题并实现帖子删除功能 -->
