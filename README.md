# radio-verified · 自有电台库

为 [本分工具箱收音机](https://app.benfen.net/radio/) 准备的自有电台数据。

- 数据源：[Radio Browser](https://www.radio-browser.info/) 社区库，多国聚合（全球热门 + 中/港/台全量 + 主要国家头部），去重约 5700 台。
- 每台逐个服务端验活，只收录能连通的，死台、落地页直接剔除。
- 每台标注播放方式（收音机据此选路，字段沿用收音机现有数据格式，另加 `mode`）：
  - `direct`：https 且带跨域头，浏览器可直连；
  - `proxy`：其余可达流，走收音机自家代理转发；
  - `hls`：m3u8 流，附 `hls:1/0` 表示能否直连 hls.js 播放。
- 每天北京时间 06:00 / 18:00 自动重新聚合验证，产物发布到 GitHub Pages。

数据地址：<https://benfennet.github.io/radio-verified/stations.json>
状态页：<https://benfennet.github.io/radio-verified/>
