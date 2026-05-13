import { useEffect, useState } from 'react'
import { App, Alert, Card, Form, Input, Select, Button, message, Tabs, Space, Tag, Typography, Modal, QRCode, Switch } from 'antd'
import {
  SaveOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  MailOutlined,
  SafetyOutlined,
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SyncOutlined,
  PlusOutlined,
  LockOutlined,
} from '@ant-design/icons'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { apiFetch } from '@/lib/utils'

const SELECT_FIELDS: Record<string, { label: string; value: string }[]> = {
  mail_provider: [
    { label: 'LuckMail（订单接码 / 已购邮箱）', value: 'luckmail' },
    { label: 'Laoudo（固定邮箱）', value: 'laoudo' },
    { label: 'TempMail.lol（自动生成）', value: 'tempmail_lol' },
    { label: 'SkyMail（CloudMail 接口）', value: 'skymail' },
    { label: 'DuckMail（自动生成）', value: 'duckmail' },
    { label: 'MoeMail (sall.cc)', value: 'moemail' },
    { label: 'YYDS Mail / MaliAPI', value: 'maliapi' },
    { label: 'GPTMail', value: 'gptmail' },
    { label: 'OpenTrashMail', value: 'opentrashmail' },
    { label: '2925 Mail (Web)', value: 'mail2925' },
    { label: 'Freemail（自建 CF Worker）', value: 'freemail' },
    { label: 'CF Worker（自建域名）', value: 'cfworker' },
  ],
  maliapi_auto_domain_strategy: [
    { label: 'balanced', value: 'balanced' },
    { label: 'prefer_owned', value: 'prefer_owned' },
    { label: 'prefer_public', value: 'prefer_public' },
  ],
  default_executor: [
    { label: 'API 协议（无浏览器）', value: 'protocol' },
    { label: '无头浏览器', value: 'headless' },
    { label: '有头浏览器', value: 'headed' },
  ],
  default_captcha_solver: [
    { label: 'YesCaptcha', value: 'yescaptcha' },
    { label: '本地 Solver (Camoufox)', value: 'local_solver' },
    { label: '手动', value: 'manual' },
  ],
  cpa_cleanup_enabled: [
    { label: '关闭', value: '0' },
    { label: '开启', value: '1' },
  ],
  codex_proxy_upload_type: [
    { label: 'AT（Access Token，推荐）', value: 'at' },
    { label: 'RT（Refresh Token）', value: 'rt' },
  ],
  smsbower_type: [
    { label: '任意质量', value: '' },
    { label: 'Gold（高成功率）', value: 'gold' },
    { label: 'Silver（标准）', value: 'silver' },
  ],
  sms_provider: [
    { label: 'SMSBOWER', value: 'smsbower' },
    { label: '5SIM', value: '5sim' },
    { label: 'HeroSMS', value: 'herosms' },
  ],
  payment_auto_plan: [
    { label: '关闭（不自动升级）', value: '' },
    { label: '注册后自动升级 Plus（$20/月）', value: 'plus' },
    { label: '注册后自动升级 Team（需填 Workspace）', value: 'team' },
  ],
  payment_plus_flow_order: [
    { label: '默认：OAuth/Token 后再升级（当前稳定模式）', value: 'after_oauth' },
    { label: 'Plus：基础账号创建后先升级，再 OAuth/上传', value: 'before_oauth' },
  ],
  payment_method: [
    { label: '信用卡 (Card)', value: 'card' },
    { label: 'GoPay (印尼电子钱包)', value: 'gopay' },
    { label: 'PayPal', value: 'paypal' },
  ],
  payment_provider: [
    { label: 'Auto by payment method', value: '' },
    { label: 'PayPal Web', value: 'paypal_web' },
    { label: 'GoPay API', value: 'gopay_api' },
    { label: 'GoPay Android Emulator', value: 'gopay_android' },
    { label: 'Card', value: 'card' },
    { label: 'Manual payment link only', value: 'manual_link' },
  ],
  payment_promo_proxy_geo: [
    { label: 'JP - Japan promo', value: 'JP' },
    { label: 'US - United States', value: 'US' },
    { label: 'IE - Ireland', value: 'IE' },
    { label: 'DE - Germany', value: 'DE' },
    { label: 'GB - United Kingdom', value: 'GB' },
    { label: 'SG - Singapore', value: 'SG' },
  ],
  payment_android_headless: [
    { label: 'Headless emulator', value: '1' },
    { label: 'Visible emulator window', value: '0' },
  ],
  payment_billing_country: [
    { label: 'US （美国）', value: 'US' },
    { label: 'GB （英国）', value: 'GB' },
    { label: 'DE （德国）', value: 'DE' },
    { label: 'JP （日本）', value: 'JP' },
    { label: 'SG （新加坡）', value: 'SG' },
    { label: 'HK （香港）', value: 'HK' },
    { label: 'AU （澳大利亚）', value: 'AU' },
    { label: 'CA （加拿大）', value: 'CA' },
    { label: 'IE （爱尔兰，PayPal 推荐）', value: 'IE' },
    { label: 'ID （印度尼西亚，GoPay 必选）', value: 'ID' },
  ],
  payment_skip_if_not_free: [
    { label: '关闭（即使非免费也付费开通）', value: '0' },
    { label: '开启 ⭐推荐（promo 未生效则保留 Free 不扣款）', value: '1' },
  ],
  payment_auto_cancel_after_subscribe: [
    { label: '关闭（订阅后不自动取消，下月会续费）', value: '0' },
    { label: '开启 ⭐推荐（开通后立即取消，保留 Plus 1 月不续费）', value: '1' },
  ],
  payment_gopay_auto_register: [
    { label: '关闭（使用手动配置的 phone+pin）', value: '0' },
    { label: '开启 ⭐ 无限循环开 Plus（每轮自动 SMSBOWER 印尼号 + Gojek 注册 + 设置 PIN）', value: '1' },
  ],
  payment_captcha_validate_online: [
    { label: '关闭（保存配置时不联网校验打码 key）', value: '0' },
    { label: '开启（自动支付前在线校验打码 key）', value: '1' },
  ],
  payment_is_coupon_from_query_param: [
    { label: '关闭（推荐，promo eligible 更稳定）', value: '0' },
    { label: '开启（按 query 参数来源提交 coupon）', value: '1' },
  ],
  payment_checkout_ui_mode: [
    { label: 'custom（嵌入式 Checkout，默认）', value: 'custom' },
    { label: 'hosted（Stripe 托管页）', value: 'hosted' },
  ],
}

const TAB_ITEMS = [
  {
    key: 'register',
    label: '注册设置',
    icon: <ApiOutlined />,
    sections: [
      {
        title: '默认注册方式',
        desc: '控制注册任务如何执行',
        fields: [{ key: 'default_executor', label: '执行器类型', type: 'select' }],
      },
    ],
  },
  {
    key: 'mailbox',
    label: '邮箱服务',
    icon: <MailOutlined />,
    sections: [
      {
        title: '默认邮箱服务',
        desc: '选择注册时使用的邮箱类型',
        fields: [{ key: 'mail_provider', label: '邮箱服务', type: 'select' }],
      },
      {
        title: 'Laoudo',
        provider: 'laoudo',
        desc: '固定邮箱，手动配置',
        fields: [
          { key: 'laoudo_email', label: '邮箱地址', placeholder: 'xxx@laoudo.com' },
          { key: 'laoudo_account_id', label: 'Account ID', placeholder: '563' },
          { key: 'laoudo_auth', label: 'JWT Token', placeholder: 'eyJ...', secret: true },
        ],
      },
      {
        title: 'Freemail',
        provider: 'freemail',
        desc: '基于 Cloudflare Worker 的自建邮箱，支持管理员令牌或账号密码认证',
        fields: [
          { key: 'freemail_api_url', label: 'API URL', placeholder: 'https://mail.example.com' },
          { key: 'freemail_admin_token', label: '管理员令牌', secret: true },
          { key: 'freemail_username', label: '用户名（可选）' },
          { key: 'freemail_password', label: '密码（可选）', secret: true },
        ],
      },
      {
        title: '2925 Mail',
        provider: 'mail2925',
        desc: 'Use 2925 webmail session inbox. Login uses the 2925 main account prefix and reads inbox via web APIs.',
        fields: [
          { key: 'mail2925_login_name', label: 'Login Name', placeholder: 'yourname' },
          { key: 'mail2925_password', label: 'Password', secret: true },
          { key: 'mail2925_alias_mode', label: 'Alias Mode', placeholder: 'plus / main / random' },
          { key: 'mail2925_domain', label: 'Alias Domain', placeholder: '2925.com' },
        ],
      },
      {
        title: 'MoeMail',
        provider: 'moemail',
        desc: '自动注册账号并生成临时邮箱',
        fields: [
          { key: 'moemail_api_url', label: 'API URL', placeholder: 'https://sall.cc' },
          { key: 'moemail_api_key', label: 'API Key', secret: true },
        ],
      },
      {
        title: 'SkyMail',
        provider: 'skymail',
        desc: 'CloudMail 兼容接口（addUser / emailList）',
        fields: [
          { key: 'skymail_api_base', label: 'API Base', placeholder: 'https://api.skymail.ink' },
          { key: 'skymail_token', label: 'Authorization Token', secret: true },
          { key: 'skymail_domain', label: '邮箱域名', placeholder: 'mail.example.com' },
        ],
      },
      {
        title: 'YYDS Mail / MaliAPI',
        provider: 'maliapi',
        desc: '基于 API Key 创建临时邮箱并轮询收件箱消息',
        fields: [
          { key: 'maliapi_base_url', label: 'API URL', placeholder: 'https://maliapi.215.im/v1' },
          { key: 'maliapi_api_key', label: 'API Key', secret: true },
          { key: 'maliapi_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
          { key: 'maliapi_auto_domain_strategy', label: '自动域名策略', type: 'select' },
        ],
      },
      {
        title: 'GPTMail',
        provider: 'gptmail',
        desc: '基于 GPTMail API 生成临时邮箱并轮询邮件；若已知本站可用域名，也可本地拼装随机地址',
        fields: [
          { key: 'gptmail_base_url', label: 'API URL', placeholder: 'https://mail.chatgpt.org.uk' },
          { key: 'gptmail_api_key', label: 'API Key', secret: true, placeholder: 'gpt-test' },
          { key: 'gptmail_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
        ],
      },
      {
        title: 'OpenTrashMail',
        provider: 'opentrashmail',
        desc: '对接 opentrashmail 服务；可直接轮询 /json/<email>，也支持已知域名时本地拼装随机地址',
        fields: [
          { key: 'opentrashmail_api_url', label: 'API URL', placeholder: 'http://mail.example.com:8085' },
          { key: 'opentrashmail_domain', label: '邮箱域名（可选）', placeholder: 'xiyoufm.com' },
          { key: 'opentrashmail_password', label: '站点密码（可选）', secret: true, placeholder: '启用 PASSWORD 时填写' },
        ],
      },
      {
        title: 'TempMail.lol',
        provider: 'tempmail_lol',
        desc: '自动生成邮箱，无需配置，需要代理访问（CN IP 被封）',
        fields: [],
      },
      {
        title: 'DuckMail',
        provider: 'duckmail',
        desc: '自动生成邮箱，随机创建账号',
        fields: [
          { key: 'duckmail_api_url', label: 'Web URL', placeholder: 'https://www.duckmail.sbs' },
          { key: 'duckmail_provider_url', label: 'Provider URL', placeholder: 'https://api.duckmail.sbs' },
          { key: 'duckmail_bearer', label: 'Bearer Token', placeholder: 'kevin273945', secret: true },
          { key: 'duckmail_domain', label: '自定义域名', placeholder: '留空则从 Provider URL 推导' },
          { key: 'duckmail_api_key', label: 'API Key（私有域名）', placeholder: 'dk_xxx（domain.duckmail.sbs 获取）', secret: true },
        ],
      },
      {
        title: 'CF Worker 自建邮箱',
        provider: 'cfworker',
        desc: '基于 Cloudflare Worker 的自建临时邮箱服务',
        fields: [
          { key: 'cfworker_api_url', label: 'API URL', placeholder: 'https://apimail.example.com' },
          { key: 'cfworker_admin_token', label: '管理员 Token', secret: true },
          { key: 'cfworker_custom_auth', label: '站点密码', secret: true },
          { key: 'cfworker_subdomain', label: '固定子域名', placeholder: 'mail / pool-a' },
          { key: 'cfworker_random_subdomain', label: '随机子域名', type: 'boolean' },
          { key: 'cfworker_fingerprint', label: 'Fingerprint', placeholder: '6703363b...' },
        ],
      },
      {
        title: 'LuckMail',
        provider: 'luckmail',
        desc: 'ChatGPT 走购买邮箱，其他平台继续走订单接码老逻辑',
        fields: [
          { key: 'luckmail_base_url', label: '平台地址', placeholder: 'https://mails.luckyous.com' },
          { key: 'luckmail_api_key', label: 'API Key', secret: true },
          { key: 'luckmail_email_type', label: '邮箱类型（可选）', placeholder: 'ms_graph / ms_imap / self_built' },
          { key: 'luckmail_domain', label: '邮箱域名（可选）', placeholder: 'outlook.com / gmail.com' },
        ],
      },
      {
        title: 'SMSBOWER 手机接码',
        desc: '用于 ChatGPT add-phone 阶段；国家代码由 SMSBOWER 提供，质量支持任意 / gold / silver',
        fields: [
          { key: 'sms_provider', label: '接码平台', type: 'select' },
          { key: 'smsbower_api_key', label: 'API Key', secret: true },
          { key: 'sim5_api_key', label: '5SIM API Key', secret: true },
          { key: 'herosms_api_key', label: 'HeroSMS API Key', secret: true },
          { key: 'smsbower_country', label: '国家代码', placeholder: '例如 12,10,22,6,52,78（美国、越南、英国、印尼、泰国、法国）' },
          { key: 'smsbower_type', label: '号码质量', type: 'select' },
          { key: 'smsbower_max_price', label: '最高单价（美元）', placeholder: '例如 0.09；买不到号时调高' },
          { key: 'smsbower_min_price', label: '最低单价（美元，可选）', placeholder: '通常留空' },
          { key: 'smsbower_phone_attempts', label: '每国最多取号次数', placeholder: '默认 12' },
          { key: 'smsbower_add_phone_send_attempts', label: 'add-phone 尝试次数', placeholder: '默认 8' },
          { key: 'smsbower_otp_timeout_seconds', label: '短信等待秒数', placeholder: '默认 120' },
          { key: 'smsbower_code_attempts', label: '验证码提交/重发次数', placeholder: '默认 2' },
          { key: 'fraud_guard_proxy_rotations', label: 'fraud_guard 换代理次数', placeholder: '默认 3' },
          { key: 'smsbower_provider_ids', label: '指定供应商 ID（可选）', placeholder: '多个用英文逗号分隔，例如 2260,2920' },
          { key: 'smsbower_except_provider_ids', label: '排除供应商 ID（可选）', placeholder: '多个用英文逗号分隔，例如 2217' },
        ],
      },
    ],
  },
  {
    key: 'captcha',
    label: '验证码',
    icon: <SafetyOutlined />,
    sections: [
      {
        title: '验证码服务',
        desc: '用于绕过注册页面的人机验证',
        fields: [
          { key: 'default_captcha_solver', label: '默认服务', type: 'select' },
          { key: 'yescaptcha_key', label: 'YesCaptcha Key', secret: true },
        ],
      },
    ],
  },
  {
    key: 'chatgpt',
    label: 'ChatGPT',
    icon: <ApiOutlined />,
    sections: [
      {
        title: 'CPA 面板',
        desc: '注册完成后自动上传到 CPA 管理平台',
        fields: [
          { key: 'cpa_api_url', label: 'API URL', placeholder: 'https://your-cpa.example.com' },
          { key: 'cpa_api_key', label: 'API Key', secret: true },
        ],
      },
      {
        title: 'Sub2API 面板',
        desc: '注册完成后自动上传到 Sub2API 管理后台',
        fields: [
          { key: 'sub2api_api_url', label: 'API URL', placeholder: 'https://your-sub2api.example.com' },
          { key: 'sub2api_api_key', label: 'API Key', secret: true },
          { key: 'sub2api_group_ids', label: '分组 ID', placeholder: '多个分组用英文逗号分隔，例如 2,4,8' },
        ],
      },
      {
        title: 'CPA 自动维护',
        desc: '定时删除 status=error 的凭证，剩余数量低于阈值时自动按现有配置补注册 ChatGPT',
        fields: [
          { key: 'cpa_cleanup_enabled', label: '自动维护', type: 'select' },
          { key: 'cpa_cleanup_interval_minutes', label: '检查间隔（分钟）', placeholder: '60' },
          { key: 'cpa_cleanup_threshold', label: '最低凭证阈值', placeholder: '5' },
          { key: 'cpa_cleanup_concurrency', label: '补注册并发数', placeholder: '1' },
          { key: 'cpa_cleanup_register_delay_seconds', label: '每个注册延迟（秒）', placeholder: '0' },
        ],
      },
      {
        title: 'Team Manager',
        desc: '上传到自建 Team Manager 系统',
        fields: [
          { key: 'team_manager_url', label: 'API URL', placeholder: 'https://your-tm.example.com' },
          { key: 'team_manager_key', label: 'API Key', secret: true },
        ],
      },
      {
        title: 'CodexProxy',
        desc: '注册完成后自动上传到 CodexProxy 管理平台',
        fields: [
          { key: 'codex_proxy_url', label: 'API URL', placeholder: 'https://your-codex-proxy.example.com' },
          { key: 'codex_proxy_key', label: 'Admin Key', secret: true },
          { key: 'codex_proxy_upload_type', label: '上传类型' },
        ],
      },
      {
        title: 'SMSToMe 手机验证',
        desc: 'ChatGPT add_phone 阶段自动取号并轮询短信验证码',
        fields: [
          { key: 'smstome_cookie', label: 'SMSToMe Cookie', secret: true },
          { key: 'smstome_country_slugs', label: '国家列表', placeholder: 'united-states,united-kingdom,poland' },
          { key: 'smstome_phone_attempts', label: '手机号尝试次数', placeholder: '3' },
          { key: 'smstome_otp_timeout_seconds', label: '短信等待秒数', placeholder: '45' },
          { key: 'smstome_poll_interval_seconds', label: '轮询间隔秒数', placeholder: '5' },
          { key: 'smstome_sync_max_pages_per_country', label: '每国同步页数', placeholder: '5' },
        ],
      },
      {
        title: '⚡ 自动支付升级配置',
        desc: '配置后注册账号将自动完成 Plus/Team 升级，无需手动操作',
        fields: [
          { key: 'payment_auto_plan', label: '自动升级套餐', type: 'select' },
          { key: 'payment_plus_flow_order', label: 'Plus 支付时机', type: 'select' },
          { key: 'payment_method', label: '支付方式', type: 'select' },
          { key: 'payment_skip_if_not_free', label: '⭐ 不免费就跳过（保留 Free）', type: 'select' },
          { key: 'payment_auto_cancel_after_subscribe', label: '⭐ 开通后自动取消订阅（防续费）', type: 'select' },
          { key: 'payment_gopay_auto_register', label: '⭐ GoPay 全自动注册（无限开 Plus）', type: 'select' },
          { key: 'payment_card_number', label: '信用卡号', placeholder: '4242424242424242', secret: true },
          { key: 'payment_card_exp_month', label: '到期月（MM）', placeholder: '12' },
          { key: 'payment_card_exp_year', label: '到期年（YYYY）', placeholder: '2027' },
          { key: 'payment_card_cvc', label: 'CVC / CVV', placeholder: '123', secret: true },
          { key: 'payment_paypal_email', label: 'PayPal 账号', placeholder: 'paypal@example.com（仅 PayPal 模式）' },
          { key: 'payment_paypal_password', label: 'PayPal 密码', secret: true, placeholder: '仅 PayPal 模式' },
          { key: 'payment_gopay_phone', label: 'GoPay 手机号', placeholder: '8123456789（印尼号无需+62前缀，仅 GoPay 模式）' },
          { key: 'payment_gopay_pin', label: 'GoPay PIN 码', secret: true, placeholder: '6位数字（仅 GoPay 模式）' },
          { key: 'payment_gopay_otp_file', label: 'GoPay 自动接码文件路径', placeholder: '留空则使用 runtime/wa_relay/wa-otp.txt' },
          { key: 'payment_gopay_otp_url', label: 'GoPay 自动接码 URL', placeholder: '例如 http://127.0.0.1:8765/latest（本地扫码中间件或 WhatsApp Webhook 都可）' },
          { key: 'payment_gopay_sms_country', label: 'GoPay 接码国家', placeholder: 'SMSBOWER 国家代码，默认 6（印尼）' },
          { key: 'payment_gopay_sms_service', label: 'GoPay 接码服务', placeholder: 'SMSBOWER 服务代码，默认 ot' },
          { key: 'wa_relay_src_dir', label: 'WhatsApp Relay 源码目录', placeholder: '例如 C:\\Desktop\\Gpt-Agreement-Payment-main\\Gpt-Agreement-Payment-main\\webui\\whatsapp_relay' },
          { key: 'wa_relay_proxy_url', label: 'WhatsApp Relay 代理', placeholder: '例如 http://127.0.0.1:7897（WhatsApp WebSocket 超时时填写）' },
          { key: 'payment_card_py_path', label: 'CTF-pay card.py 路径', placeholder: '例如 C:\\Desktop\\Gpt-Agreement-Payment-main\\Gpt-Agreement-Payment-main\\CTF-pay\\card.py' },
          { key: 'payment_python_executable', label: '支付独立 Python', placeholder: '例如 E:\\ctf-pay\\python.exe（留空则复用当前后端环境）' },
          { key: 'payment_captcha_validate_online', label: '在线校验打码 Key', type: 'select' },
          { key: 'payment_is_coupon_from_query_param', label: 'Coupon 来源参数', type: 'select' },
          { key: 'payment_checkout_ui_mode', label: 'Checkout UI 模式', type: 'select' },
          { key: 'payment_vlm_base_url', label: 'hCaptcha VLM Base URL', placeholder: '例如 https://api.openai.com/v1 或 https://lucen.cc' },
          { key: 'payment_vlm_api_key', label: 'hCaptcha VLM API Key', secret: true },
          { key: 'payment_vlm_model', label: 'hCaptcha VLM 模型', placeholder: 'gpt-4o' },
          { key: 'payment_vlm_timeout_s', label: 'hCaptcha VLM 超时秒数', placeholder: '45' },
          { key: 'payment_billing_name', label: '持卡人姓名', placeholder: 'John Smith' },
          { key: 'payment_billing_country', label: '账单国家', type: 'select' },
          { key: 'payment_billing_address', label: '账单地址', placeholder: '123 Main St' },
          { key: 'payment_billing_city', label: '城市', placeholder: 'New York' },
          { key: 'payment_billing_state', label: '州/省份', placeholder: 'NY' },
          { key: 'payment_billing_zip', label: '邮政编码', placeholder: '10001' },
          { key: 'payment_captcha_api_url', label: 'hCaptcha 打码平台 URL', placeholder: 'https://api.yescaptcha.com（兼容 YesCaptcha 协议）' },
          { key: 'payment_captcha_key', label: '打码平台 Client Key', secret: true },
          { key: 'payment_team_workspace_name', label: 'Team Workspace 名称', placeholder: 'MyWorkspace（仅 Team 套餐需填）' },
          { key: 'payment_team_seat_quantity', label: 'Team 席位数量', placeholder: '5（仅 Team 套餐需填）' },
        ],
      },
    ],
  },
  {
    key: 'cliproxyapi',
    label: 'CLIProxyAPI',
    icon: <ApiOutlined />,
    sections: [
      {
        title: '管理面板',
        desc: '用于 CLIProxyAPI 管理页登录',
        fields: [
          { key: 'cliproxyapi_base_url', label: 'API URL', placeholder: 'http://127.0.0.1:8317' },
          { key: 'cliproxyapi_management_key', label: '管理口令', secret: true, placeholder: '默认 cliproxyapi' },
        ],
      },
    ],
  },
  {
    key: 'grok',
    label: 'Grok',
    icon: <ApiOutlined />,
    sections: [
      {
        title: 'grok2api',
        desc: '注册成功后自动导入到 grok2api 管理后台',
        fields: [
          { key: 'grok2api_url', label: 'API URL', placeholder: 'http://127.0.0.1:7860' },
          { key: 'grok2api_app_key', label: 'App Key', secret: true },
          { key: 'grok2api_pool', label: 'Token Pool', placeholder: 'ssoBasic 或 ssoSuper' },
          { key: 'grok2api_quota', label: 'Quota（可选）', placeholder: '留空按池默认值' },
        ],
      },
    ],
  },
  {
    key: 'kiro',
    label: 'Kiro',
    icon: <ApiOutlined />,
    sections: [
      {
        title: 'Kiro Account Manager',
        desc: '注册成功后自动写入 kiro-account-manager 的 accounts.json',
        fields: [
          {
            key: 'kiro_manager_path',
            label: 'accounts.json 路径（可选）',
            placeholder: '留空则自动使用系统默认路径',
          },
          {
            key: 'kiro_manager_exe',
            label: 'Kiro Manager 可执行文件（可选）',
            placeholder: '未安装 Rust 时可填写已安装的 KiroAccountManager.exe',
          },
        ],
      },
    ],
  },
  {
    key: 'integrations',
    label: '插件',
    icon: <ApiOutlined />,
    sections: [],
  },
  {
    key: 'security',
    label: '安全',
    icon: <LockOutlined />,
    sections: [],
  },
]

interface FieldConfig {
  key: string
  label: string
  placeholder?: string
  type?: 'select' | 'input' | 'boolean'
  secret?: boolean
}

interface SectionConfig {
  title: string
  desc?: string
  fields: FieldConfig[]
  provider?: string
}

interface TabConfig {
  key: string
  label: string
  icon: React.ReactNode
  sections: SectionConfig[]
}

const EXTRA_PAYMENT_FIELDS: FieldConfig[] = [
  { key: 'payment_provider', label: 'Payment Provider Route', type: 'select' },
  { key: 'payment_promo_proxy_url', label: 'Japan/Promo Proxy URL', placeholder: 'socks5://jp-user:pass@host:port or http://host:port' },
  { key: 'payment_promo_proxy_geo', label: 'Promo Proxy Geo', type: 'select' },
  { key: 'payment_paypal_proxy_url', label: 'PayPal Dedicated Proxy URL', placeholder: 'Optional; ignored when promo proxy is set' },
  { key: 'payment_proxy_pool', label: 'Retry Proxy Pool', placeholder: 'Comma-separated proxies for retry/IP rotation' },
  { key: 'payment_max_retries', label: 'Payment Max Retries', placeholder: '2' },
  { key: 'payment_gopay_otp_retries', label: 'GoPay OTP Retries', placeholder: '2' },
  { key: 'payment_gojek_app_version', label: 'Gojek App Version', placeholder: 'Optional API header override' },
  { key: 'payment_android_avd_name', label: 'Android AVD Name', placeholder: 'Pixel_8_Play or your Google Play image AVD' },
  { key: 'payment_android_serial', label: 'Android Device Serial', placeholder: 'emulator-5554 (optional)' },
  { key: 'payment_android_headless', label: 'Android Emulator Mode', type: 'select' },
  { key: 'payment_android_gojek_apk', label: 'Gojek APK Path', placeholder: 'Optional local APK path' },
  { key: 'payment_android_gopay_apk', label: 'GoPay APK Path', placeholder: 'Optional local APK path' },
  { key: 'payment_android_adb_path', label: 'ADB Path', placeholder: 'Optional adb.exe path' },
  { key: 'payment_android_emulator_path', label: 'Emulator Path', placeholder: 'Optional emulator.exe path' },
]

function hasValue(value: unknown) {
  if (value === null || value === undefined) return false
  if (typeof value === 'string') return value.trim().length > 0
  return true
}

function PaymentUpgradeSummary({ form }: { form: any }) {
  const plan = String(Form.useWatch('payment_auto_plan', form) || '').trim()
  const plusFlowOrder = String(Form.useWatch('payment_plus_flow_order', form) || 'after_oauth').trim()
  const method = String(Form.useWatch('payment_method', form) || '').trim()
  const cardNumber = Form.useWatch('payment_card_number', form)
  const cardExpMonth = Form.useWatch('payment_card_exp_month', form)
  const cardExpYear = Form.useWatch('payment_card_exp_year', form)
  const cardCvc = Form.useWatch('payment_card_cvc', form)
  const billingName = Form.useWatch('payment_billing_name', form)
  const billingCountry = Form.useWatch('payment_billing_country', form)
  const paypalEmail = Form.useWatch('payment_paypal_email', form)
  const paypalPassword = Form.useWatch('payment_paypal_password', form)
  const gopayPhone = Form.useWatch('payment_gopay_phone', form)
  const gopayPin = Form.useWatch('payment_gopay_pin', form)
  const gopayAutoRegister = String(Form.useWatch('payment_gopay_auto_register', form) || '0').trim()
  const gopayOtpFile = Form.useWatch('payment_gopay_otp_file', form)
  const gopayOtpUrl = Form.useWatch('payment_gopay_otp_url', form)
  const teamWorkspaceName = Form.useWatch('payment_team_workspace_name', form)

  const planLabel =
    plan === 'plus'
      ? plusFlowOrder === 'before_oauth'
        ? '基础账号后先升级 Plus，再 OAuth'
        : '注册后自动升级 Plus'
      : plan === 'team'
        ? '注册后自动升级 Team'
        : '只注册，不自动升级'

  const methodLabel =
    method === 'card'
      ? '信用卡'
      : method === 'gopay'
        ? 'GoPay'
        : method === 'paypal'
          ? 'PayPal'
          : '未设置'

  const warnings: string[] = []
  if (plan) {
    if (!method) {
      warnings.push('已开启自动升级，但还没有选择支付方式。')
    }
    if (method === 'card') {
      const missing: string[] = []
      if (!hasValue(cardNumber)) missing.push('卡号')
      if (!hasValue(cardExpMonth)) missing.push('到期月')
      if (!hasValue(cardExpYear)) missing.push('到期年')
      if (!hasValue(cardCvc)) missing.push('CVC')
      if (!hasValue(billingName)) missing.push('持卡人姓名')
      if (!hasValue(billingCountry)) missing.push('账单国家')
      if (missing.length > 0) {
        warnings.push(`信用卡信息未填完整：${missing.join('、')}`)
      }
    }
    if (method === 'gopay') {
      const missing: string[] = []
      if (gopayAutoRegister !== '1') {
        if (!hasValue(gopayPhone)) missing.push('GoPay 手机号')
        if (!hasValue(gopayPin)) missing.push('GoPay PIN')
      }
      if (missing.length > 0) {
        warnings.push(`GoPay 信息未填完整：${missing.join('、')}`)
      }
      if (!hasValue(gopayOtpUrl) && !hasValue(gopayOtpFile)) {
        warnings.push('未填写 GoPay OTP URL/文件时，将回退使用 runtime/wa_relay/wa-otp.txt。')
      }
    }
    if (method === 'paypal') {
      const missing: string[] = []
      if (!hasValue(paypalEmail)) missing.push('PayPal 账号')
      if (!hasValue(paypalPassword)) missing.push('PayPal 密码')
      if (missing.length > 0) {
        warnings.push(`PayPal 信息未填完整：${missing.join('、')}`)
      }
    }
    if (plan === 'team' && !hasValue(teamWorkspaceName)) {
      warnings.push('Team 模式还没有填写 Workspace 名称。')
    }
    if (plan === 'team' && plusFlowOrder === 'before_oauth') {
      warnings.push('“OAuth 前升级”只对 Plus 生效；Team 仍会按原链路在 OAuth/Token 后升级。')
    }
  }

  const alertType = !plan ? 'info' : warnings.length > 0 ? 'warning' : 'success'

  return (
    <Alert
      type={alertType}
      showIcon
      style={{ marginBottom: 16 }}
      message={`当前模式：${planLabel}`}
      description={
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <Space wrap>
            <Tag color={!plan ? 'blue' : plan === 'plus' ? 'gold' : 'purple'}>{plan || 'register-only'}</Tag>
            {plan === 'plus' ? (
              <Tag color={plusFlowOrder === 'before_oauth' ? 'orange' : 'cyan'}>
                支付时机：{plusFlowOrder === 'before_oauth' ? 'OAuth 前' : 'OAuth 后'}
              </Tag>
            ) : null}
            <Tag color={method ? 'geekblue' : 'default'}>支付方式：{methodLabel}</Tag>
            {plan === 'team' ? (
              <Tag color={hasValue(teamWorkspaceName) ? 'success' : 'warning'}>
                Workspace：{hasValue(teamWorkspaceName) ? '已配置' : '未配置'}
              </Tag>
            ) : null}
            {method === 'gopay' ? (
              <Tag color={hasValue(gopayOtpUrl) || hasValue(gopayOtpFile) ? 'success' : 'processing'}>
                OTP：{hasValue(gopayOtpUrl) ? 'URL' : hasValue(gopayOtpFile) ? '文件' : 'WA Relay 默认文件'}
              </Tag>
            ) : null}
          </Space>
          {warnings.length > 0 ? (
            <div>
              {warnings.map((warning) => (
                <div key={warning} style={{ color: '#ad6800' }}>
                  {warning}
                </div>
              ))}
            </div>
          ) : (
            <Typography.Text type="secondary">
              {!plan ? '当前保存的是只注册模式，注册成功后不会自动升级 Plus 或 Team。' : '当前配置看起来可用于自动升级。'}
            </Typography.Text>
          )}
        </div>
      }
    />
  )
}

function formatResultText(data: unknown) {
  if (typeof data === 'string') return data
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}

function normalizeDomainList(input: unknown): string[] {
  const items = Array.isArray(input) ? input : []
  const seen = new Set<string>()
  const domains: string[] = []
  for (const item of items) {
    const domain = String(item || '').trim().toLowerCase().replace(/^@/, '')
    if (!domain || seen.has(domain)) continue
    seen.add(domain)
    domains.push(domain)
  }
  return domains
}

function parseStoredDomainList(value: unknown): string[] {
  if (Array.isArray(value)) return normalizeDomainList(value)
  if (typeof value !== 'string') return []

  const text = value.trim()
  if (!text) return []

  try {
    const parsed = JSON.parse(text)
    if (Array.isArray(parsed)) {
      return normalizeDomainList(parsed)
    }
  } catch {}

  return normalizeDomainList(
    text
      .split('\n')
      .flatMap((line) => line.split(','))
      .map((item) => item.trim()),
  )
}

function ConfigField({ field }: { field: FieldConfig }) {
  const [showSecret, setShowSecret] = useState(false)
  const options = SELECT_FIELDS[field.key]
  const isBooleanField = field.type === 'boolean'
  const helpText =
    field.key === 'default_executor'
      ? '仅对支持的平台生效；ChatGPT、Cursor、Grok、Kiro、Tavily、Trae 支持浏览器模式，OpenBlockLabs 仅支持纯协议。'
      : field.key === 'payment_plus_flow_order'
        ? '默认保持现有链路；选择 OAuth 前时，仅 Plus 会在基础账号创建完成后先走支付，再继续 OAuth/token 和上传。'
      : field.key === 'payment_card_py_path'
        ? '用于调用外部 CTF-pay/card.py。留空时会先读 CARD_PY_PATH，再尝试项目相邻目录和内置默认路径。'
      : field.key === 'payment_gopay_otp_file'
        ? '留空时，GoPay 会自动回退到 runtime/wa_relay/wa-otp.txt，适配内置 WhatsApp Relay。'
      : undefined

  return (
    <Form.Item
      label={field.label}
      name={field.key}
      extra={helpText}
      valuePropName={isBooleanField ? 'checked' : undefined}
    >
      {options ? (
        <Select options={options} style={{ width: '100%' }} />
      ) : isBooleanField ? (
        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
      ) : field.secret ? (
        <Input.Password
          placeholder={field.placeholder}
          visibilityToggle={{
            visible: !showSecret,
            onVisibleChange: setShowSecret,
          }}
          iconRender={(visible) => (visible ? <EyeOutlined /> : <EyeInvisibleOutlined />)}
        />
      ) : (
        <Input placeholder={field.placeholder} />
      )}
    </Form.Item>
  )
}

function ConfigSection({ form, section }: { form: any; section: SectionConfig }) {
  const showPaymentSummary = section.fields.some((field) => field.key === 'payment_auto_plan')
  const allFields = showPaymentSummary ? [...section.fields, ...EXTRA_PAYMENT_FIELDS] : section.fields

  return (
    <Card title={section.title} extra={section.desc && <span style={{ fontSize: 12, color: '#7a8ba3' }}>{section.desc}</span>} style={{ marginBottom: 16 }}>
      {showPaymentSummary ? <PaymentUpgradeSummary form={form} /> : null}
      {allFields.map((field) => (
        <ConfigField key={field.key} field={field} />
      ))}
    </Card>
  )
}

function MailboxSections({ form, sections }: { form: any; sections: SectionConfig[] }) {
  const selectedProvider = Form.useWatch('mail_provider', form) || 'luckmail'
  const baseSections = sections.filter((section) => !section.provider)
  const providerSections = sections.filter((section) => section.provider)
  const activeProviderSection =
    providerSections.find((section) => section.provider === selectedProvider) || providerSections[0]

  return (
    <>
      {baseSections.map((section) => (
        <ConfigSection key={section.title} form={form} section={section} />
      ))}

      {activeProviderSection ? (
        <Card
          title={activeProviderSection.title}
          extra={activeProviderSection.desc && <span style={{ fontSize: 12, color: '#7a8ba3' }}>{activeProviderSection.desc}</span>}
          style={{ marginBottom: 16 }}
        >
          {activeProviderSection.fields.length > 0 ? (
            activeProviderSection.fields.map((field) => <ConfigField key={field.key} field={field} />)
          ) : (
            <Typography.Text type="secondary">当前邮箱服务无需额外配置。</Typography.Text>
          )}
        </Card>
      ) : null}
    </>
  )
}

function CFWorkerDomainPoolSection({ form }: { form: any }) {
  const watchedDomains = Form.useWatch('cfworker_domains', form) || []
  const watchedEnabledDomains = Form.useWatch('cfworker_enabled_domains', form) || []
  const normalizedDomains = normalizeDomainList(watchedDomains)
  const enabledDomains = normalizeDomainList(watchedEnabledDomains).filter((domain) => normalizedDomains.includes(domain))

  const updateEnabledDomains = (nextDomains: string[]) => {
    form.setFieldValue('cfworker_enabled_domains', normalizeDomainList(nextDomains))
  }

  const toggleEnabledDomain = (domain: string, checked: boolean) => {
    if (checked) {
      updateEnabledDomains([...enabledDomains, domain])
      return
    }
    updateEnabledDomains(enabledDomains.filter((item) => item !== domain))
  }

  return (
    <Card
      title="CF Worker 域名池"
      extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>注册时会从已启用域名中随机选择一个</span>}
      style={{ marginBottom: 16 }}
    >
      <Form.List name="cfworker_domains">
        {(fields, { add, remove }) => (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {fields.map((field) => (
              <Space key={field.key} align="start" style={{ display: 'flex' }}>
                <Form.Item
                  {...field}
                  label={field.name === 0 ? '全部域名' : ''}
                  style={{ flex: 1, marginBottom: 0 }}
                  rules={[
                    {
                      validator: async (_, value) => {
                        if (!String(value || '').trim()) {
                          throw new Error('请输入域名')
                        }
                      },
                    },
                  ]}
                >
                  <Input placeholder="example.com" />
                </Form.Item>
                <Button
                  danger
                  onClick={() => {
                    const currentDomains = Array.isArray(form.getFieldValue('cfworker_domains'))
                      ? [...form.getFieldValue('cfworker_domains')]
                      : []
                    const removedDomain = String(currentDomains[field.name] || '').trim().toLowerCase().replace(/^@/, '')
                    remove(field.name)
                    if (!removedDomain) return
                    const enabledDomains = normalizeDomainList(form.getFieldValue('cfworker_enabled_domains'))
                    form.setFieldValue(
                      'cfworker_enabled_domains',
                      enabledDomains.filter((domain) => domain !== removedDomain),
                    )
                  }}
                >
                  删除
                </Button>
              </Space>
            ))}
            {fields.length === 0 ? (
              <Typography.Text type="secondary">还没有配置域名。添加后即可在下方选择启用项。</Typography.Text>
            ) : null}
            <Button type="dashed" onClick={() => add('')} icon={<PlusOutlined />} block>
              添加域名
            </Button>
          </div>
        )}
      </Form.List>

      <Form.Item name="cfworker_enabled_domains" hidden>
        <Select mode="multiple" options={normalizedDomains.map((domain) => ({ label: domain, value: domain }))} />
      </Form.Item>

      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>已启用域名</div>
        {enabledDomains.length > 0 ? (
          <Space wrap>
            {enabledDomains.map((domain) => (
              <Tag
                key={domain}
                color="blue"
                closable
                onClose={(event) => {
                  event.preventDefault()
                  updateEnabledDomains(enabledDomains.filter((item) => item !== domain))
                }}
              >
                {domain}
              </Tag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">暂无启用域名，点击下方域名即可启用。</Typography.Text>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>点击切换启用状态</div>
        {normalizedDomains.length > 0 ? (
          <Space wrap>
            {normalizedDomains.map((domain) => (
              <Tag.CheckableTag
                key={domain}
                checked={enabledDomains.includes(domain)}
                onChange={(checked) => toggleEnabledDomain(domain, checked)}
              >
                {domain}
              </Tag.CheckableTag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">请先在上方添加域名。</Typography.Text>
        )}
      </div>
      <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
        仅已启用域名会参与注册；点击已启用标签可直接移除。
      </Typography.Text>
    </Card>
  )
}

function SolverStatus() {
  const [running, setRunning] = useState<boolean | null>(null)

  const checkSolver = async () => {
    try {
      const d = await apiFetch('/solver/status')
      setRunning(d.running)
    } catch {
      setRunning(false)
    }
  }

  const restartSolver = async () => {
    await apiFetch('/solver/restart', { method: 'POST' })
    setRunning(null)
    setTimeout(checkSolver, 2000)
  }

  useEffect(() => {
    checkSolver()
    const timer = window.setInterval(checkSolver, 5000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <Card title="Turnstile Solver" size="small" style={{ marginBottom: 16 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <Space size={8}>
          {running === null ? (
            <SyncOutlined spin style={{ color: '#7a8ba3' }} />
          ) : running ? (
            <CheckCircleOutlined style={{ color: '#10b981' }} />
          ) : (
            <CloseCircleOutlined style={{ color: '#ef4444' }} />
          )}
          <span style={{ color: running ? '#10b981' : '#7a8ba3', fontWeight: 500 }}>
            {running === null ? '检测中' : running ? '运行中' : '未运行'}
          </span>
        </Space>
        <Button size="small" onClick={restartSolver}>
          重启 Solver
        </Button>
      </div>
    </Card>
  )
}

function WaRelayStatus() {
  const [info, setInfo] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [pairingPhone, setPairingPhone] = useState('')

  const refresh = async () => {
    try {
      const d = await apiFetch('/wa_relay/status')
      setInfo(d)
    } catch {
      setInfo({ process_running: false, logged_in: false })
    }
  }

  const restart = async (login_mode: 'qr' | 'pairing' = 'qr') => {
    setLoading(true)
    try {
      const body: any = { login_mode }
      if (login_mode === 'pairing' && pairingPhone) {
        body.pairing_phone = pairingPhone.replace(/\D/g, '')
      }
      await apiFetch('/wa_relay/restart', { method: 'POST', body: JSON.stringify(body) })
      message.success('已重启 WhatsApp Relay，请等待几秒...')
      setTimeout(refresh, 3000)
    } finally {
      setLoading(false)
    }
  }

  const logout = async () => {
    Modal.confirm({
      title: '确认注销 WhatsApp？',
      content: '将清除 session，需要重新扫码或配对登录',
      onOk: async () => {
        await apiFetch('/wa_relay/logout', { method: 'POST' })
        message.success('已注销，正在重启 Relay...')
        setTimeout(refresh, 3000)
      },
    })
  }

  useEffect(() => {
    refresh()
    const timer = window.setInterval(refresh, 3000)
    return () => window.clearInterval(timer)
  }, [])

  if (!info) {
    return null
  }

  const status = String(info.status || '').toLowerCase()
  const connected = !!info.logged_in
  const running = !!info.process_running
  const showQR = !connected && (!!info.qr_data_url || !!info.qr_text)
  const showPairing = !connected && !!info.pairing_code
  const latestOtp = info.latest_otp || ''
  const latestOtpTime = info.latest_otp_time
    ? new Date(Number(info.latest_otp_time) * 1000).toLocaleString()
    : ''
  const relayError = String(info.error || '')
  const relayReason = String(info.reason || '')
  const relayConnectionTimedOut = /handshake|timed out|connection closed|websocket/i.test(`${relayError} ${relayReason}`)

  return (
    <Card
      title="📱 WhatsApp Relay（GoPay OTP 全自动）"
      size="small"
      style={{ marginBottom: 16 }}
      extra={
        <Space size={6}>
          {connected ? (
            <CheckCircleOutlined style={{ color: '#10b981' }} />
          ) : running ? (
            <SyncOutlined spin style={{ color: '#3b82f6' }} />
          ) : (
            <CloseCircleOutlined style={{ color: '#ef4444' }} />
          )}
          <span style={{ fontWeight: 500, color: connected ? '#10b981' : running ? '#3b82f6' : '#ef4444' }}>
            {connected ? '已登录' : status || (running ? '启动中' : '未运行')}
          </span>
        </Space>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {!info.node_available && (
          <Alert type="error" message="未找到 Node.js" description="WhatsApp Relay 需要 Node.js v18+。安装后重启后端或点击重启 Relay。" />
        )}

        {info.node_available && info.node_path && (
          <Alert type="success" message="Node.js 已找到" description={`当前路径: ${info.node_path}`} />
        )}

        {info.node_available && !info.relay_src_exists && (
          <Alert
            type="error"
            message="WhatsApp Relay 源码目录不存在"
            description={`当前目录: ${info.relay_src_dir || '未配置'}。请在自动支付配置中设置 WhatsApp Relay 源码目录，或设置 WA_RELAY_SRC_DIR。`}
          />
        )}

        {showQR && (
          <Alert
            type="warning"
            message="请用 WhatsApp 扫描二维码登录"
            description={
              <div style={{ marginTop: 8, textAlign: 'center' }}>
                {info.qr_data_url ? (
                  <img
                    src={info.qr_data_url}
                    alt="WhatsApp QR"
                    style={{ width: 240, height: 240, background: '#fff', padding: 8, borderRadius: 8 }}
                  />
                ) : (
                  <QRCode value={String(info.qr_text || '')} size={240} />
                )}
                <div style={{ marginTop: 8, color: '#7a8ba3', fontSize: 12 }}>
                  WhatsApp → 设置 → 已链接的设备 → 链接设备
                </div>
              </div>
            }
          />
        )}

        {showPairing && (
          <Alert
            type="info"
            message="请在 WhatsApp 输入配对码"
            description={
              <div style={{ marginTop: 8 }}>
                <div
                  style={{
                    fontSize: 32,
                    fontWeight: 'bold',
                    letterSpacing: 4,
                    textAlign: 'center',
                    fontFamily: 'monospace',
                    background: '#fff',
                    padding: 12,
                    borderRadius: 8,
                  }}
                >
                  {info.pairing_code}
                </div>
                <div style={{ marginTop: 8, color: '#7a8ba3', fontSize: 12 }}>
                  WhatsApp → 设置 → 已链接的设备 → 链接设备 → 用电话号码链接
                </div>
              </div>
            }
          />
        )}

        {connected && latestOtp && (
          <Alert
            type="success"
            message={`最近 OTP: ${latestOtp}`}
            description={`捕获时间: ${latestOtpTime}`}
          />
        )}

        {info.error && (
          <Alert
            type="error"
            message="Relay 错误"
            description={
              relayConnectionTimedOut
                ? `${relayError}。这是 WhatsApp WebSocket 连接失败，请在自动支付配置里填写 WhatsApp Relay 代理，例如 http://127.0.0.1:7897，保存后点重启（QR 模式）。`
                : relayError
            }
          />
        )}

        <Space wrap>
          <Button size="small" loading={loading} onClick={() => restart('qr')}>
            🔄 重启（QR 模式）
          </Button>
          <Input
            size="small"
            placeholder="手机号 如 8615870862693"
            value={pairingPhone}
            onChange={(e) => setPairingPhone(e.target.value)}
            style={{ width: 200 }}
          />
          <Button size="small" loading={loading} onClick={() => restart('pairing')} disabled={!pairingPhone}>
            🔢 重启（配对码模式）
          </Button>
          {connected && (
            <Button size="small" danger onClick={logout}>
              注销 WhatsApp
            </Button>
          )}
        </Space>

        <div style={{ fontSize: 12, color: '#7a8ba3' }}>
          OTP 文件: <code>{info.otp_file}</code>
        </div>
      </div>
    </Card>
  )
}

function IntegrationsPanel() {
  const [items, setItems] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [resultModal, setResultModal] = useState({
    open: false,
    title: '',
    ok: true,
    content: '',
  })

  const showResultModal = (title: string, data: unknown, ok = true) => {
    setResultModal({
      open: true,
      title,
      ok,
      content: formatResultText(data),
    })
  }

  const load = async () => {
    setLoading(true)
    try {
      const d = await apiFetch('/integrations/services')
      setItems(d.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [])

  const doAction = async (key: string, request: Promise<any>) => {
    setBusy(key)
    try {
      const result = await request
      await load()
      message.success('操作完成')
      showResultModal('操作结果', result, true)
    } catch (e: any) {
      message.error(e?.message || '操作失败')
      showResultModal('操作结果', e?.message || e || '操作失败', false)
      await load()
    } finally {
      setBusy('')
    }
  }

  const backfill = async (platforms: string[], label: string, busyKey: string) => {
    setBusy(busyKey)
    try {
      const d = await apiFetch('/integrations/backfill', {
        method: 'POST',
        body: JSON.stringify({ platforms }),
      })
      message.success(`${label} 回填完成：成功 ${d.success} / ${d.total}`)
      showResultModal(`${label} 回填结果`, d, true)
    } catch (e: any) {
      message.error(e?.message || `${label} 回填失败`)
      showResultModal(`${label} 回填结果`, e?.message || e || `${label} 回填失败`, false)
    } finally {
      setBusy('')
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Modal
        open={resultModal.open}
        title={resultModal.title}
        onCancel={() => setResultModal((v) => ({ ...v, open: false }))}
        onOk={() => setResultModal((v) => ({ ...v, open: false }))}
        width={760}
      >
        <Typography.Paragraph style={{ marginBottom: 8, color: resultModal.ok ? '#10b981' : '#ef4444' }}>
          {resultModal.ok ? '操作已完成。' : '操作失败。'}
        </Typography.Paragraph>
        <pre
          style={{
            margin: 0,
            maxHeight: 420,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'rgba(127,127,127,0.08)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {resultModal.content}
        </pre>
      </Modal>

      <Card title="批量操作">
        <Space wrap>
          <Button loading={busy === 'start-all'} onClick={() => doAction('start-all', apiFetch('/integrations/services/start-all', { method: 'POST' }))}>
            启动全部（已安装）
          </Button>
          <Button loading={busy === 'stop-all'} onClick={() => doAction('stop-all', apiFetch('/integrations/services/stop-all', { method: 'POST' }))}>
            停止全部
          </Button>
          <Button loading={loading} onClick={load}>
            刷新状态
          </Button>
        </Space>
      </Card>

      {items.map((item) => (
        <Card key={item.name} title={item.label}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <div>
              状态：
              <Tag color={item.running ? 'green' : 'default'} style={{ marginLeft: 8 }}>
                {item.running ? '运行中' : '未运行'}
              </Tag>
              <Tag color={item.repo_exists ? 'blue' : 'orange'} style={{ marginLeft: 8 }}>
                {item.repo_exists ? '已安装' : '未安装'}
              </Tag>
              {item.pid ? <span style={{ marginLeft: 8 }}>PID: {item.pid}</span> : null}
            </div>
            <div>插件目录：<Typography.Text copyable>{item.repo_path}</Typography.Text></div>
            {item.url ? <div>地址：<Typography.Text copyable>{item.url}</Typography.Text></div> : null}
            {item.management_url ? <div>管理页：<Typography.Text copyable>{item.management_url}</Typography.Text></div> : null}
            {item.management_key ? <div>登录口令：<Typography.Text copyable>{item.management_key}</Typography.Text></div> : null}
            <div>日志：<Typography.Text copyable>{item.log_path}</Typography.Text></div>
            {item.last_error ? <div style={{ color: '#ef4444' }}>最近错误：{item.last_error}</div> : null}
            <Space wrap>
              {item.management_url ? (
                <Button onClick={() => window.open(item.management_url, '_blank')}>
                  打开管理页
                </Button>
              ) : null}
              {!item.repo_exists ? (
                <Button
                  type="primary"
                  loading={busy === `install-${item.name}`}
                  onClick={() => doAction(`install-${item.name}`, apiFetch(`/integrations/services/${item.name}/install`, { method: 'POST' }))}
                >
                  安装
                </Button>
              ) : null}
              <Button
                loading={busy === `start-${item.name}`}
                disabled={!item.repo_exists}
                onClick={() => doAction(`start-${item.name}`, apiFetch(`/integrations/services/${item.name}/start`, { method: 'POST' }))}
              >
                启动
              </Button>
              <Button
                loading={busy === `stop-${item.name}`}
                onClick={() => doAction(`stop-${item.name}`, apiFetch(`/integrations/services/${item.name}/stop`, { method: 'POST' }))}
              >
                停止
              </Button>
              {item.name === 'grok2api' ? (
                <Button
                  loading={busy === 'backfill-grok'}
                  onClick={() => backfill(['grok'], 'Grok', 'backfill-grok')}
                >
                  回填现有 Grok 账号
                </Button>
              ) : null}
              {item.name === 'kiro-manager' ? (
                <Button
                  loading={busy === 'backfill-kiro'}
                  onClick={() => backfill(['kiro'], 'Kiro', 'backfill-kiro')}
                >
                  回填现有 Kiro 账号
                </Button>
              ) : null}
            </Space>
          </Space>
        </Card>
      ))}
    </div>
  )
}

type TotpSetupState = 'idle' | 'setup'

function SecurityPanel() {
  const { message: msg } = App.useApp()
  const [status, setStatus] = useState<{ has_password: boolean; has_totp: boolean } | null>(null)
  const [loading, setLoading] = useState(false)

  const [enableForm] = Form.useForm()
  const [pwForm] = Form.useForm()
  const [codeForm] = Form.useForm()

  const [totpSetupState, setTotpSetupState] = useState<TotpSetupState>('idle')
  const [totpSecret, setTotpSecret] = useState('')
  const [totpUri, setTotpUri] = useState('')

  const loadStatus = async () => {
    try {
      const s = await apiFetch('/auth/status')
      setStatus(s)
    } catch {}
  }

  useEffect(() => { loadStatus() }, [])

  const handleEnable = async (values: { password: string; confirm: string }) => {
    if (values.password !== values.confirm) {
      msg.error('两次输入的密码不一致')
      return
    }
    setLoading(true)
    try {
      const d = await apiFetch('/auth/setup', {
        method: 'POST',
        body: JSON.stringify({ password: values.password }),
      })
      localStorage.setItem('auth_token', d.access_token)
      msg.success('密码保护已启用')
      enableForm.resetFields()
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisableAuth = async () => {
    setLoading(true)
    try {
      await apiFetch('/auth/disable', { method: 'POST' })
      localStorage.removeItem('auth_token')
      msg.success('密码保护已关闭')
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleChangePassword = async (values: { current_password: string; new_password: string; confirm: string }) => {
    if (values.new_password !== values.confirm) {
      msg.error('两次输入的新密码不一致')
      return
    }
    setLoading(true)
    try {
      await apiFetch('/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ current_password: values.current_password, new_password: values.new_password }),
      })
      msg.success('密码已更新')
      pwForm.resetFields()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleSetupTotp = async () => {
    setLoading(true)
    try {
      const d = await apiFetch('/auth/2fa/setup')
      setTotpSecret(d.secret)
      setTotpUri(d.uri)
      setTotpSetupState('setup')
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleEnableTotp = async (values: { code: string }) => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/enable', {
        method: 'POST',
        body: JSON.stringify({ secret: totpSecret, code: values.code }),
      })
      msg.success('双因素认证已启用')
      setTotpSetupState('idle')
      codeForm.resetFields()
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisableTotp = async () => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/disable', { method: 'POST' })
      msg.success('双因素认证已关闭')
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title="访问密码保护"
        extra={
          status?.has_password
            ? <Tag color="green"><CheckCircleOutlined /> 已启用</Tag>
            : <Tag color="default"><CloseCircleOutlined /> 未启用</Tag>
        }
      >
        {!status?.has_password ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text type="secondary">
              启用后，访问页面需要输入密码。默认不开启，任何能访问此地址的人均可使用。
            </Typography.Text>
            <Form form={enableForm} layout="vertical" onFinish={handleEnable} requiredMark={false} style={{ maxWidth: 360, marginTop: 8 }}>
              <Form.Item name="password" label="设置访问密码" rules={[{ required: true, message: '请输入密码' }, { min: 6, message: '至少 6 位' }]}>
                <Input.Password placeholder="至少 6 位" />
              </Form.Item>
              <Form.Item name="confirm" label="确认密码" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入密码" />
              </Form.Item>
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" loading={loading} icon={<LockOutlined />}>
                  启用密码保护
                </Button>
              </Form.Item>
            </Form>
          </Space>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text type="secondary">当前已启用密码保护，关闭后任何人无需密码即可访问。</Typography.Text>
            <Button danger loading={loading} onClick={handleDisableAuth}>
              关闭密码保护
            </Button>
          </Space>
        )}
      </Card>

      {status?.has_password && (
        <>
          <Card title="修改密码">
            <Form form={pwForm} layout="vertical" onFinish={handleChangePassword} requiredMark={false} style={{ maxWidth: 360 }}>
              <Form.Item name="current_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}>
                <Input.Password placeholder="当前密码" />
              </Form.Item>
              <Form.Item name="new_password" label="新密码" rules={[{ required: true, message: '请输入新密码' }, { min: 6, message: '至少 6 位' }]}>
                <Input.Password placeholder="新密码（至少 6 位）" />
              </Form.Item>
              <Form.Item name="confirm" label="确认新密码" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入新密码" />
              </Form.Item>
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" loading={loading} icon={<SaveOutlined />}>
                  更新密码
                </Button>
              </Form.Item>
            </Form>
          </Card>

          <Card
            title="双因素认证 (2FA)"
            extra={
              status?.has_totp
                ? <Tag color="green"><CheckCircleOutlined /> 已启用</Tag>
                : <Tag color="default"><CloseCircleOutlined /> 未启用</Tag>
            }
          >
            {status?.has_totp ? (
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  登录时需输入 Google Authenticator / Authy 等 App 中的 6 位验证码。
                </Typography.Text>
                <Button danger loading={loading} onClick={handleDisableTotp}>
                  关闭双因素认证
                </Button>
              </Space>
            ) : totpSetupState === 'idle' ? (
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  启用后，登录时除密码外还需输入验证器 App 中的 6 位验证码，大幅提升安全性。
                </Typography.Text>
                <Button type="primary" loading={loading} onClick={handleSetupTotp} icon={<SafetyOutlined />}>
                  开启双因素认证
                </Button>
              </Space>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Typography.Text strong>1. 用验证器 App 扫描下方二维码</Typography.Text>
                <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                  <QRCode value={totpUri} size={180} />
                  <div style={{ flex: 1 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>无法扫码？手动输入密钥：</Typography.Text>
                    <Typography.Paragraph copyable style={{ fontFamily: 'monospace', fontSize: 13, marginTop: 4 }}>
                      {totpSecret}
                    </Typography.Paragraph>
                  </div>
                </div>
                <Typography.Text strong>2. 输入 App 中显示的 6 位验证码以确认绑定</Typography.Text>
                <Form form={codeForm} layout="inline" onFinish={handleEnableTotp}>
                  <Form.Item name="code" rules={[{ required: true, message: '请输入验证码' }, { len: 6, message: '6 位数字' }]}>
                    <Input placeholder="000000" maxLength={6} style={{ width: 140, letterSpacing: 4, textAlign: 'center' }} />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit" loading={loading}>确认启用</Button>
                  </Form.Item>
                  <Form.Item>
                    <Button onClick={() => setTotpSetupState('idle')}>取消</Button>
                  </Form.Item>
                </Form>
              </Space>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

export default function Settings() {
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [activeTab, setActiveTab] = useState('register')

  useEffect(() => {
    apiFetch('/config').then((data) => {
      if (!data.mail_provider) {
        data.mail_provider = 'luckmail'
      }
      if (!data.gptmail_base_url) {
        data.gptmail_base_url = 'https://mail.chatgpt.org.uk'
      }
      if (!data.maliapi_base_url) {
        data.maliapi_base_url = 'https://maliapi.215.im/v1'
      }
      if (!data.luckmail_base_url) {
        data.luckmail_base_url = 'https://mails.luckyous.com/'
      }
      data.cfworker_domains = parseStoredDomainList(data.cfworker_domains)
      data.cfworker_enabled_domains = parseStoredDomainList(data.cfworker_enabled_domains)
      data.cfworker_random_subdomain = parseBooleanConfigValue(data.cfworker_random_subdomain)
      form.setFieldsValue(data)
    })
  }, [form])

  const save = async () => {
    setSaving(true)
    try {
      const values = form.getFieldsValue(true)
      const domains = normalizeDomainList(values.cfworker_domains)
      const enabledDomains = normalizeDomainList(values.cfworker_enabled_domains).filter((domain) => domains.includes(domain))

      if (domains.length > 0 && enabledDomains.length === 0) {
        setActiveTab('mailbox')
        message.error('CF Worker 至少需要启用一个域名')
        return
      }

      values.cfworker_domains = JSON.stringify(domains)
      values.cfworker_enabled_domains = JSON.stringify(enabledDomains)
      if (domains.length > 0) {
        values.cfworker_domain = ''
      }
      values.cfworker_random_subdomain = parseBooleanConfigValue(values.cfworker_random_subdomain)

      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data: values }) })
      form.setFieldsValue({
        cfworker_domains: domains,
        cfworker_enabled_domains: enabledDomains,
        cfworker_domain: domains.length > 0 ? '' : values.cfworker_domain,
        cfworker_random_subdomain: values.cfworker_random_subdomain,
      })
      message.success('保存成功')
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  const currentTab = TAB_ITEMS.find((t) => t.key === activeTab) as TabConfig
  const selectedMailProvider = Form.useWatch('mail_provider', form) || 'luckmail'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>全局配置</h1>
        <p style={{ color: '#7a8ba3', marginTop: 4 }}>配置将持久化保存，注册任务自动使用</p>
      </div>

      <div style={{ display: 'flex', gap: 24 }}>
        <div style={{ width: 200 }}>
          <Tabs
            tabPosition="left"
            activeKey={activeTab}
            onChange={setActiveTab}
            items={TAB_ITEMS.map((t) => ({
              key: t.key,
              label: (
                <span>
                  {t.icon}
                  <span style={{ marginLeft: 8 }}>{t.label}</span>
                </span>
              ),
            }))}
          />
        </div>

        <div style={{ flex: 1 }}>
          {activeTab === 'integrations' ? (
            <IntegrationsPanel />
          ) : activeTab === 'security' ? (
            <SecurityPanel />
          ) : (
            <Form form={form} layout="vertical">
              {activeTab === 'captcha' ? <SolverStatus /> : null}
              {activeTab === 'chatgpt' ? <WaRelayStatus /> : null}
              {activeTab === 'mailbox' ? (
                <>
                  <MailboxSections form={form} sections={currentTab.sections} />
                  {selectedMailProvider === 'cfworker' ? <CFWorkerDomainPoolSection form={form} /> : null}
                </>
              ) : (
                currentTab.sections.map((section) => <ConfigSection key={section.title} form={form} section={section} />)
              )}
              <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} block>
                {saved ? '已保存 ✓' : '保存配置'}
              </Button>
            </Form>
          )}
        </div>
      </div>
    </div>
  )
}
