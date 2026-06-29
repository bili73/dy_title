抖音拍同款抓取 - 使用说明
========================================

【前置准备】（只做一次）
1. 确保 docker 命令可用：装 Docker Desktop 或 WSL2 里跑 docker engine 都行，
   验证：cmd 跑 `docker info` 能返回 Server 信息（不报错）。docker.exe 要在系统 PATH。
   docker daemon 必须在跑（启动 Docker Desktop，或 wsl 里启动 dockerd）。
2. 手机开启「USB 调试」，用数据线连电脑（手机弹窗点「允许」调试授权）
3. 手机上「抖音商城 (livelite)」App 登录一次

【日常使用】
1. 双击「抖音抓取.exe」
   - 首次启动会自动加载 paddleocr.tar 部署 OCR（约 1-2 分钟，之后秒起）
   - 之后自动打开浏览器进入控制台
2. 控制台操作：
   - 上传要搜索的商品图片
   - 选「进详情抓参数」模式 + 填「参数容器关键词」
     （轮胎：类型,速度级别 / 手机：维修方式,上市时间 / 充气泵：电压,适用对象）
   - 设置商品数量 → 点「开始抓取」
3. 结果在 output/ 目录（Excel/JSON/TXT），控制台可下载
4. 换手机或相册改版导致首图点不准时：点「校准相册首图」，进相册后在截图上点一下首图位置即可

【文件夹内容（请勿删除/改名）】
- 抖音抓取.exe        主程序（双击启动）
- paddleocr.tar       OCR 镜像（首次自动加载，请留在 exe 同目录）
- adb.exe + *.dll     手机连接工具
- _internal/          程序依赖库
- images/             默认搜索图（也可在控制台上传覆盖）
- output/             抓取结果输出目录

【常见问题】
- 双击 exe 没反应/闪退 → 打开 cmd，拖 exe 进去回车，看错误信息
- 提示「Docker 未运行」→ 启动 docker 服务（Docker Desktop，或 wsl 里启动 dockerd），
  等 daemon 起来后验证 `docker info` 能返回 Server 信息，再双击 exe
- 提示「未找到 paddleocr.tar」→ 确认 paddleocr.tar 和 exe 在同一文件夹
- 手机连不上 / 抓取卡住 → 检查手机 USB 调试是否开启、数据线是否正常、抖音商城是否登录
- OCR 端口 9300 被占用 → 关掉占用 9300 的程序，或 docker rm -f dev-paddleocr 后重启 exe
