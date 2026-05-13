import { useEffect, useState } from 'react'
import {
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  Button,
  Checkbox,
  Tag,
  Space,
  Typography,
  Descriptions,
} from 'antd'
import {
  PlayCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
} from '@ant-design/icons'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { getExecutorOptions, normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'
import { apiFetch } from '@/lib/utils'
import { useRegisterTask } from '@/contexts/RegisterTaskContext'

const { Text } = Typography
const DEFAULT_PARALLEL_MAIL_MIX = ['luckmail', 'cfworker', 'mail2925']
const MIX_PROVIDER_OPTIONS = [
  { value: 'luckmail', label: 'LuckMail' },
  { value: 'cfworker', label: 'CF Worker' },
  { value: 'mail2925', label: '2925 Mail' },
  { value: 'moemail', label: 'MoeMail (sall.cc)' },
  { value: 'tempmail_lol', label: 'TempMail.lol' },
  { value: 'skymail', label: 'SkyMail (CloudMail)' },
  { value: 'maliapi', label: 'YYDS Mail / MaliAPI' },
  { value: 'gptmail', label: 'GPTMail' },
  { value: 'opentrashmail', label: 'OpenTrashMail' },
  { value: 'duckmail', label: 'DuckMail' },
  { value: 'freemail', label: 'Freemail' },
  { value: 'laoudo', label: 'Laoudo' },
]

export default function RegisterTaskPage() {
  const [form] = Form.useForm()
  const [mixProviderOptions, setMixProviderOptions] = useState(MIX_PROVIDER_OPTIONS)
  const { task, polling, startTask, clearTask } = useRegisterTask()
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()

  useEffect(() => {
    apiFetch('/config').then((cfg) => {
      const configuredProviders = MIX_PROVIDER_OPTIONS.filter((item) => {
        if (item.value === 'luckmail') return !!String(cfg.luckmail_api_key || '').trim()
        if (item.value === 'cfworker') return !!String(cfg.cfworker_api_url || '').trim()
        if (item.value === 'mail2925') return !!String(cfg.mail2925_login_name || '').trim() && !!String(cfg.mail2925_password || '').trim()
        if (item.value === 'moemail') return !!String(cfg.moemail_api_url || '').trim() && !!String(cfg.moemail_api_key || '').trim()
        if (item.value === 'tempmail_lol') return true
        if (item.value === 'skymail') return !!String(cfg.skymail_api_base || '').trim() && !!String(cfg.skymail_token || '').trim()
        if (item.value === 'maliapi') return !!String(cfg.maliapi_base_url || '').trim() && !!String(cfg.maliapi_api_key || '').trim()
        if (item.value === 'gptmail') return !!String(cfg.gptmail_base_url || '').trim() && !!String(cfg.gptmail_api_key || '').trim()
        if (item.value === 'opentrashmail') return !!String(cfg.opentrashmail_api_url || '').trim()
        if (item.value === 'duckmail') return !!String(cfg.duckmail_api_url || '').trim() || !!String(cfg.duckmail_provider_url || '').trim()
        if (item.value === 'freemail') return !!String(cfg.freemail_api_url || '').trim()
        if (item.value === 'laoudo') return !!String(cfg.laoudo_email || '').trim() && !!String(cfg.laoudo_auth || '').trim()
        return false
      })
      const resolvedMix = configuredProviders.length > 0 ? configuredProviders : MIX_PROVIDER_OPTIONS.filter((item) => DEFAULT_PARALLEL_MAIL_MIX.includes(item.value))
      setMixProviderOptions(resolvedMix)
      const currentPlatform = form.getFieldValue('platform') || 'trae'
      form.setFieldsValue({
        executor_type: normalizeExecutorForPlatform(currentPlatform, cfg.default_executor),
        captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
        mail_provider: cfg.mail_provider || 'luckmail',
        yescaptcha_key: cfg.yescaptcha_key || '',
        moemail_api_url: cfg.moemail_api_url || '',
        moemail_api_key: cfg.moemail_api_key || '',
        skymail_api_base: cfg.skymail_api_base || 'https://api.skymail.ink',
        skymail_token: cfg.skymail_token || '',
        skymail_domain: cfg.skymail_domain || '',
        laoudo_auth: cfg.laoudo_auth || '',
        laoudo_email: cfg.laoudo_email || '',
        laoudo_account_id: cfg.laoudo_account_id || '',
        gptmail_base_url: cfg.gptmail_base_url || 'https://mail.chatgpt.org.uk',
        gptmail_api_key: cfg.gptmail_api_key || '',
        gptmail_domain: cfg.gptmail_domain || '',
        opentrashmail_api_url: cfg.opentrashmail_api_url || '',
        opentrashmail_domain: cfg.opentrashmail_domain || '',
        opentrashmail_password: cfg.opentrashmail_password || '',
        maliapi_base_url: cfg.maliapi_base_url || 'https://maliapi.215.im/v1',
        maliapi_api_key: cfg.maliapi_api_key || '',
        maliapi_domain: cfg.maliapi_domain || '',
        maliapi_auto_domain_strategy: cfg.maliapi_auto_domain_strategy || 'balanced',
        duckmail_api_url: cfg.duckmail_api_url || '',
        duckmail_provider_url: cfg.duckmail_provider_url || '',
        duckmail_bearer: cfg.duckmail_bearer || '',
        freemail_api_url: cfg.freemail_api_url || '',
        freemail_admin_token: cfg.freemail_admin_token || '',
        freemail_username: cfg.freemail_username || '',
        freemail_password: cfg.freemail_password || '',
        mail2925_login_name: cfg.mail2925_login_name || '',
        mail2925_password: cfg.mail2925_password || '',
        mail2925_alias_mode: cfg.mail2925_alias_mode || 'main',
        mail2925_domain: cfg.mail2925_domain || '2925.com',
        mail_provider_mix: resolvedMix.map((item) => item.value),
        cfworker_api_url: cfg.cfworker_api_url || '',
        cfworker_admin_token: cfg.cfworker_admin_token || '',
        cfworker_custom_auth: cfg.cfworker_custom_auth || '',
        cfworker_domain_override: '',
        cfworker_subdomain: cfg.cfworker_subdomain || '',
        cfworker_random_subdomain: parseBooleanConfigValue(cfg.cfworker_random_subdomain),
        cfworker_fingerprint: cfg.cfworker_fingerprint || '',
        smsbower_api_key: cfg.smsbower_api_key || '',
        sms_provider: cfg.sms_provider || 'smsbower',
        sim5_api_key: cfg.sim5_api_key || '',
        herosms_api_key: cfg.herosms_api_key || '',
        smsbower_country: cfg.smsbower_country || '',
        smsbower_type: cfg.smsbower_type || '',
        smsbower_max_price: cfg.smsbower_max_price || '',
        smsbower_min_price: cfg.smsbower_min_price || '',
        smsbower_price_steps: cfg.smsbower_price_steps || '',
        smsbower_phone_attempts: cfg.smsbower_phone_attempts || '',
        smsbower_add_phone_send_attempts: cfg.smsbower_add_phone_send_attempts || '',
        smsbower_otp_timeout_seconds: cfg.smsbower_otp_timeout_seconds || '',
        smsbower_code_attempts: cfg.smsbower_code_attempts || '',
        fraud_guard_proxy_rotations: cfg.fraud_guard_proxy_rotations || '',
        smsbower_provider_ids: cfg.smsbower_provider_ids || '',
        smsbower_except_provider_ids: cfg.smsbower_except_provider_ids || '',
        luckmail_base_url: cfg.luckmail_base_url || 'https://mails.luckyous.com/',
        luckmail_api_key: cfg.luckmail_api_key || '',
        luckmail_email_type: cfg.luckmail_email_type || '',
        luckmail_domain: cfg.luckmail_domain || '',
        // 自动上传配置
        cpa_api_url: cfg.cpa_api_url || '',
        cpa_api_key: cfg.cpa_api_key || '',
        sub2api_api_url: cfg.sub2api_api_url || '',
        sub2api_api_key: cfg.sub2api_api_key || '',
        sub2api_group_ids: cfg.sub2api_group_ids || '',
        codex_proxy_url: cfg.codex_proxy_url || '',
        codex_proxy_key: cfg.codex_proxy_key || '',
        codex_proxy_upload_type: cfg.codex_proxy_upload_type || 'at',
        team_manager_url: cfg.team_manager_url || '',
        team_manager_key: cfg.team_manager_key || '',
      })
    })
  }, [form])

  const submit = async () => {
    const values = await form.validateFields()
    const registerExtra = {
      mail_provider: values.mail_provider,
      mail_provider_mix: values.mail_provider_mix_enabled ? values.mail_provider_mix : [],
      laoudo_auth: values.laoudo_auth,
      laoudo_email: values.laoudo_email,
      laoudo_account_id: values.laoudo_account_id,
      gptmail_base_url: values.gptmail_base_url,
      gptmail_api_key: values.gptmail_api_key,
      gptmail_domain: values.gptmail_domain,
      opentrashmail_api_url: values.opentrashmail_api_url,
      opentrashmail_domain: values.opentrashmail_domain,
      opentrashmail_password: values.opentrashmail_password,
      maliapi_base_url: values.maliapi_base_url,
      maliapi_api_key: values.maliapi_api_key,
      maliapi_domain: values.maliapi_domain,
      maliapi_auto_domain_strategy: values.maliapi_auto_domain_strategy,
      moemail_api_url: values.moemail_api_url,
      moemail_api_key: values.moemail_api_key,
      skymail_api_base: values.skymail_api_base,
      skymail_token: values.skymail_token,
      skymail_domain: values.skymail_domain,
      duckmail_api_url: values.duckmail_api_url,
      duckmail_provider_url: values.duckmail_provider_url,
      duckmail_bearer: values.duckmail_bearer,
      freemail_api_url: values.freemail_api_url,
      freemail_admin_token: values.freemail_admin_token,
      freemail_username: values.freemail_username,
      freemail_password: values.freemail_password,
      mail2925_login_name: values.mail2925_login_name,
      mail2925_password: values.mail2925_password,
      mail2925_alias_mode: values.mail2925_alias_mode,
      mail2925_domain: values.mail2925_domain,
      cfworker_api_url: values.cfworker_api_url,
      cfworker_admin_token: values.cfworker_admin_token,
      cfworker_custom_auth: values.cfworker_custom_auth,
      cfworker_domain_override: values.cfworker_domain_override,
      cfworker_subdomain: values.cfworker_subdomain,
      cfworker_random_subdomain: values.cfworker_random_subdomain,
      cfworker_fingerprint: values.cfworker_fingerprint,
      smsbower_api_key: values.smsbower_api_key,
      sms_provider: values.sms_provider,
      sim5_api_key: values.sim5_api_key,
      herosms_api_key: values.herosms_api_key,
      smsbower_country: values.smsbower_country,
      smsbower_type: values.smsbower_type,
      smsbower_max_price: values.smsbower_max_price,
      smsbower_min_price: values.smsbower_min_price,
      smsbower_price_steps: values.smsbower_price_steps,
      smsbower_phone_attempts: values.smsbower_phone_attempts,
      smsbower_add_phone_send_attempts: values.smsbower_add_phone_send_attempts,
      smsbower_otp_timeout_seconds: values.smsbower_otp_timeout_seconds,
      smsbower_code_attempts: values.smsbower_code_attempts,
      fraud_guard_proxy_rotations: values.fraud_guard_proxy_rotations,
      smsbower_provider_ids: values.smsbower_provider_ids,
      smsbower_except_provider_ids: values.smsbower_except_provider_ids,
      luckmail_base_url: values.luckmail_base_url,
      luckmail_api_key: values.luckmail_api_key,
      luckmail_email_type: values.luckmail_email_type,
      luckmail_domain: values.luckmail_domain,
      yescaptcha_key: values.yescaptcha_key,
      solver_url: values.solver_url,
      // 自动上传配置
      cpa_api_url: values.cpa_api_url,
      cpa_api_key: values.cpa_api_key,
      sub2api_api_url: values.sub2api_api_url,
      sub2api_api_key: values.sub2api_api_key,
      sub2api_group_ids: values.sub2api_group_ids,
      codex_proxy_url: values.codex_proxy_url,
      codex_proxy_key: values.codex_proxy_key,
      codex_proxy_upload_type: values.codex_proxy_upload_type,
      team_manager_url: values.team_manager_url,
      team_manager_key: values.team_manager_key,
    }
    const chatgptRegistrationRequestAdapter =
      buildChatGPTRegistrationRequestAdapter(
        values.platform,
        chatgptRegistrationMode,
      )
    const adaptedRegisterExtra = chatgptRegistrationRequestAdapter
      ? chatgptRegistrationRequestAdapter.extendExtra(registerExtra)
      : registerExtra

    const res = await apiFetch('/tasks/register', {
      method: 'POST',
      body: JSON.stringify({
        platform: values.platform,
        email: values.email || null,
        password: values.password || null,
        count: values.count,
        concurrency: values.concurrency,
        register_delay_seconds: values.register_delay_seconds || 0,
        proxy: values.proxy || null,
        executor_type: values.executor_type,
        captcha_solver: values.captcha_solver,
        extra: adaptedRegisterExtra,
      }),
    })
    startTask(res)
  }

  const mailProvider = Form.useWatch('mail_provider', form)
  const mailProviderMixEnabled = Form.useWatch('mail_provider_mix_enabled', form)
  const mailProviderMix = Form.useWatch('mail_provider_mix', form) || []
  const captchaSolver = Form.useWatch('captcha_solver', form)
  const platform = Form.useWatch('platform', form)
  const executorOptions = getExecutorOptions(platform)
  const selectedMailProviders = mailProviderMixEnabled
    ? Array.isArray(mailProviderMix) && mailProviderMix.length > 0
      ? mailProviderMix
      : mixProviderOptions.map((item) => item.value)
    : [mailProvider]

  useEffect(() => {
    const currentExecutor = form.getFieldValue('executor_type')
    const normalizedExecutor = normalizeExecutorForPlatform(platform, currentExecutor)
    if (currentExecutor !== normalizedExecutor) {
      form.setFieldValue('executor_type', normalizedExecutor)
    }
  }, [form, platform])

  useEffect(() => {
    if (!mailProviderMixEnabled) return
    const currentMix = form.getFieldValue('mail_provider_mix')
    if (!Array.isArray(currentMix) || currentMix.length === 0) {
      form.setFieldValue('mail_provider_mix', mixProviderOptions.map((item) => item.value))
    }
  }, [form, mailProviderMixEnabled, mixProviderOptions])

  return (
    <div style={{ maxWidth: 800 }}>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>注册任务</h1>
        <p style={{ color: '#7a8ba3', marginTop: 4 }}>创建账号自动注册任务</p>
      </div>

      <Form form={form} layout="vertical" onFinish={submit} initialValues={{
        platform: 'trae',
        executor_type: 'protocol',
        captcha_solver: 'yescaptcha',
        mail_provider: 'luckmail',
        mail_config_override_enabled: false,
        mail_provider_mix_enabled: false,
        mail_provider_mix: DEFAULT_PARALLEL_MAIL_MIX,
        gptmail_base_url: 'https://mail.chatgpt.org.uk',
        count: 1,
        concurrency: 1,
        register_delay_seconds: 0,
        maliapi_base_url: 'https://maliapi.215.im/v1',
        maliapi_auto_domain_strategy: 'balanced',
        solver_url: 'http://localhost:8889',
      }}>
        <Card title="基本配置" style={{ marginBottom: 16 }}>
          <Form.Item name="platform" label="平台" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'chatgpt', label: 'ChatGPT' },
                { value: 'trae', label: 'Trae.ai' },
                { value: 'cursor', label: 'Cursor' },
                { value: 'kiro', label: 'Kiro' },
                { value: 'grok', label: 'Grok' },
                { value: 'tavily', label: 'Tavily' },
                { value: 'openblocklabs', label: 'OpenBlockLabs' },
              ]}
            />
          </Form.Item>
          <Form.Item name="executor_type" label="执行器" rules={[{ required: true }]}>
            <Select options={executorOptions} />
          </Form.Item>
          <Form.Item name="captcha_solver" label="验证码" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'yescaptcha', label: platform === 'chatgpt' ? 'ChatGPT 默认浏览器验证链' : 'YesCaptcha' },
                { value: 'local_solver', label: '本地 Solver (Camoufox)' },
                { value: 'manual', label: '手动' },
              ]}
            />
            {platform === 'chatgpt' && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                ChatGPT 注册主要使用浏览器表单、Sentinel 与 Cloudflare warmup；本地 Solver 主要用于 Turnstile 平台。
              </Text>
            )}
          </Form.Item>
          <Space style={{ width: '100%' }}>
            <Form.Item name="count" label="批量数量" style={{ flex: 1 }}>
              <Input type="number" min={1} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数" style={{ flex: 1 }}>
              <Input type="number" min={1} max={50} />
            </Form.Item>
          </Space>
          <Space style={{ width: '100%' }}>
            <Form.Item name="register_delay_seconds" label="每个注册延迟(秒)" style={{ flex: 1 }}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0" />
            </Form.Item>
            <Form.Item name="proxy" label="代理 (可选)" style={{ flex: 1 }}>
              <Input placeholder="http://user:pass@host:port" />
            </Form.Item>
          </Space>
          {platform === 'chatgpt' && (
            <Form.Item label="ChatGPT Token 方案">
              <ChatGPTRegistrationModeSwitch
                mode={chatgptRegistrationMode}
                onChange={setChatgptRegistrationMode}
              />
            </Form.Item>
          )}
        </Card>

        <Card title="邮箱配置" style={{ marginBottom: 16 }}>
          <Form.Item name="mail_provider" label="邮箱服务" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'luckmail', label: 'LuckMail' },
                { value: 'luckmail,cfworker', label: 'LuckMail + CF Worker 混用' },
                { value: 'moemail', label: 'MoeMail (sall.cc)' },
                { value: 'tempmail_lol', label: 'TempMail.lol' },
                { value: 'skymail', label: 'SkyMail (CloudMail)' },
                { value: 'maliapi', label: 'YYDS Mail / MaliAPI' },
                { value: 'gptmail', label: 'GPTMail' },
                { value: 'opentrashmail', label: 'OpenTrashMail' },
                { value: 'mail2925', label: '2925 Mail (Web)' },
                { value: 'duckmail', label: 'DuckMail' },
                { value: 'freemail', label: 'Freemail' },
                { value: 'laoudo', label: 'Laoudo' },
                { value: 'cfworker', label: 'CF Worker' },
              ]}
            />
          </Form.Item>
          <Form.Item name="mail_provider_mix_enabled" valuePropName="checked">
            <Checkbox>启用并行邮箱混用</Checkbox>
          </Form.Item>
          {mailProviderMixEnabled && (
            <Form.Item
              name="mail_provider_mix"
              label="混用邮箱池"
              rules={[
                {
                  validator: (_, value) =>
                    Array.isArray(value) && value.length > 0
                      ? Promise.resolve()
                      : Promise.reject(new Error('请至少勾选一个邮箱源')),
                },
              ]}
              extra="并发任务会先随机打散一次，再按轮询方式分配这些邮箱源"
            >
              <Checkbox.Group
                options={mixProviderOptions}
              />
            </Form.Item>
          )}
          {selectedMailProviders.includes('skymail') && (
            <>
              <Form.Item name="skymail_api_base" label="API Base">
                <Input placeholder="https://api.skymail.ink" />
              </Form.Item>
              <Form.Item name="skymail_token" label="Authorization Token">
                <Input.Password placeholder="Bearer xxxxx" />
              </Form.Item>
              <Form.Item name="skymail_domain" label="邮箱域名">
                <Input placeholder="mail.example.com" />
              </Form.Item>
            </>
          )}
          {selectedMailProviders.includes('laoudo') && (
            <>
              <Form.Item name="laoudo_email" label="邮箱地址">
                <Input placeholder="xxx@laoudo.com" />
              </Form.Item>
              <Form.Item name="laoudo_account_id" label="Account ID">
                <Input placeholder="563" />
              </Form.Item>
              <Form.Item name="laoudo_auth" label="JWT Token">
                <Input placeholder="eyJ..." />
              </Form.Item>
            </>
          )}
          {selectedMailProviders.includes('maliapi') && (
            <>
              <Form.Item name="maliapi_base_url" label="API URL">
                <Input placeholder="https://maliapi.215.im/v1" />
              </Form.Item>
              <Form.Item name="maliapi_api_key" label="API Key">
                <Input.Password placeholder="AC-..." />
              </Form.Item>
              <Form.Item name="maliapi_domain" label="邮箱域名（可选）">
                <Input placeholder="example.com" />
              </Form.Item>
              <Form.Item name="maliapi_auto_domain_strategy" label="自动域名策略">
                <Select
                  options={[
                    { value: 'balanced', label: 'balanced' },
                    { value: 'prefer_owned', label: 'prefer_owned' },
                    { value: 'prefer_public', label: 'prefer_public' },
                  ]}
                />
              </Form.Item>
            </>
          )}
          {selectedMailProviders.includes('gptmail') && (
            <>
              <Form.Item name="gptmail_base_url" label="API URL">
                <Input placeholder="https://mail.chatgpt.org.uk" />
              </Form.Item>
              <Form.Item name="gptmail_api_key" label="API Key">
                <Input.Password placeholder="gpt-test" />
              </Form.Item>
              <Form.Item
                name="gptmail_domain"
                label="邮箱域名（可选）"
                extra="已知当前可用域名时可直接本地拼装随机地址，省掉一次 generate-email 请求"
              >
                <Input placeholder="example.com" />
              </Form.Item>
            </>
          )}
          {selectedMailProviders.includes('opentrashmail') && (
            <>
              <Form.Item name="opentrashmail_api_url" label="API URL" rules={[{ required: true, message: '请输入 OpenTrashMail 地址' }]}>
                <Input placeholder="http://mail.example.com:8085" />
              </Form.Item>
              <Form.Item
                name="opentrashmail_domain"
                label="邮箱域名（可选）"
                extra="已知 OpenTrashMail 当前启用域名时可直接本地拼装随机地址；留空则调用 /api/random 自动获取"
              >
                <Input placeholder="xiyoufm.com" />
              </Form.Item>
              <Form.Item
                name="opentrashmail_password"
                label="站点密码（可选）"
                extra="当 OpenTrashMail 开启 PASSWORD 保护时填写，会自动追加到 JSON API 查询参数"
              >
                <Input.Password placeholder="留空表示未启用" />
              </Form.Item>
            </>
          )}
          {selectedMailProviders.includes('mail2925') && (
            <>
              <Form.Item name="mail2925_login_name" label="2925 Login Name" rules={[{ required: true, message: 'Please enter 2925 login name' }]}>
                <Input placeholder="yourname" />
              </Form.Item>
              <Form.Item name="mail2925_password" label="2925 Password" rules={[{ required: true, message: 'Please enter password' }]}>
                <Input.Password placeholder="password" />
              </Form.Item>
              <Form.Item name="mail2925_alias_mode" label="Alias Mode" extra="plus = login+random@2925.com, main = fixed main mailbox, random = random local part">
                <Select
                  options={[
                    { value: 'plus', label: 'plus' },
                    { value: 'main', label: 'main' },
                    { value: 'random', label: 'random' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="mail2925_domain" label="Alias Domain">
                <Input placeholder="2925.com" />
              </Form.Item>
            </>
          )}
          {selectedMailProviders.includes('cfworker') && (
            <>
              <Form.Item name="cfworker_api_url" label="API URL">
                <Input placeholder="https://apimail.example.com" />
              </Form.Item>
              <Form.Item name="cfworker_admin_token" label="Admin Token">
                <Input placeholder="abc123,,,abc" />
              </Form.Item>
              <Form.Item name="cfworker_custom_auth" label="Site Password">
                <Input.Password placeholder="private site password" />
              </Form.Item>
              <Form.Item
                name="cfworker_domain_override"
                label="单次任务指定域名（可选）"
                extra="留空时将从设置页已启用的域名列表中随机选择。"
              >
                <Input placeholder="example.com" />
              </Form.Item>
              <Form.Item
                name="cfworker_subdomain"
                label="子域名（可选）"
                extra="填写后将生成 xxx@子域名.根域名；若启用随机子域名，则会生成 xxx@随机值.子域名.根域名。"
              >
                <Input placeholder="mail / pool-a" />
              </Form.Item>
              <Form.Item name="cfworker_random_subdomain" label="随机子域名" valuePropName="checked">
                <Checkbox>每次注册前随机生成一层子域名</Checkbox>
              </Form.Item>
              <Form.Item name="cfworker_fingerprint" label="Fingerprint (可选)">
                <Input placeholder="cfb82279f..." />
              </Form.Item>
            </>
          )}
          {selectedMailProviders.includes('luckmail') && (
            <>
              <Form.Item name="luckmail_base_url" label="平台地址">
                <Input placeholder="https://mails.luckyous.com" />
              </Form.Item>
              <Form.Item name="luckmail_api_key" label="API Key">
                <Input.Password placeholder="ak_..." />
              </Form.Item>
              <Form.Item name="luckmail_email_type" label="邮箱类型（可选）">
                <Input placeholder="ms_graph / ms_imap" />
              </Form.Item>
              <Form.Item name="luckmail_domain" label="邮箱域名（可选）">
                <Input placeholder="outlook.com" />
              </Form.Item>
            </>
          )}
        </Card>

        {platform === 'chatgpt' && (
          <Card title="ChatGPT 手机验证（接码平台）" style={{ marginBottom: 16 }}>
            <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
              仅在 OAuth 流程进入 `add_phone` 时使用。Free 注册通常不会触发；Plus/风控场景可能需要自动接码。
            </Text>
            <Form.Item name="sms_provider" label="接码平台">
              <Select
                options={[
                  { value: 'smsbower', label: 'SMSBOWER' },
                  { value: '5sim', label: '5SIM' },
                  { value: 'herosms', label: 'HeroSMS' },
                ]}
              />
            </Form.Item>
            <Form.Item name="smsbower_api_key" label="SMSBOWER API Key">
              <Input.Password placeholder="留空则从设置页读取" />
            </Form.Item>
            <Form.Item name="sim5_api_key" label="5SIM API Key">
              <Input.Password placeholder="仅 sms_provider=5sim 时使用" />
            </Form.Item>
            <Form.Item name="herosms_api_key" label="HeroSMS API Key">
              <Input.Password placeholder="仅 sms_provider=herosms 时使用" />
            </Form.Item>
            <Form.Item name="smsbower_country" label="国家代码">
              <Input placeholder="例如 78,10,6,22,73,16,187,52,12" />
            </Form.Item>
            <Form.Item name="smsbower_type" label="号码质量">
              <Select
                options={[
                  { value: '', label: '任意质量' },
                  { value: 'gold', label: 'Gold（高成功率）' },
                  { value: 'silver', label: 'Silver（标准）' },
                ]}
              />
            </Form.Item>
            <Space style={{ width: '100%' }}>
              <Form.Item name="smsbower_max_price" label="最高单价（美元）" style={{ flex: 1 }}>
                <Input placeholder="例如 0.09" />
              </Form.Item>
              <Form.Item name="smsbower_min_price" label="最低单价（可选）" style={{ flex: 1 }}>
                <Input placeholder="通常留空" />
              </Form.Item>
            </Space>
            <Form.Item name="smsbower_price_steps" label="价格阶梯（可选）">
              <Input placeholder="例如 0.06,0.08,0.10；无号时按阶梯提价重试" />
            </Form.Item>
            <Space style={{ width: '100%' }}>
              <Form.Item name="smsbower_phone_attempts" label="每国取号次数" style={{ flex: 1 }}>
                <Input placeholder="默认 12" />
              </Form.Item>
              <Form.Item name="smsbower_add_phone_send_attempts" label="add-phone 尝试次数" style={{ flex: 1 }}>
                <Input placeholder="默认 8" />
              </Form.Item>
              <Form.Item name="smsbower_otp_timeout_seconds" label="短信等待秒数" style={{ flex: 1 }}>
                <Input placeholder="默认 120" />
              </Form.Item>
              <Form.Item name="smsbower_code_attempts" label="验证码提交次数" style={{ flex: 1 }}>
                <Input placeholder="默认 2" />
              </Form.Item>
              <Form.Item name="fraud_guard_proxy_rotations" label="fraud_guard 换代理次数" style={{ flex: 1 }}>
                <Input placeholder="默认 3" />
              </Form.Item>
            </Space>
            <Form.Item name="smsbower_provider_ids" label="指定供应商 ID（可选）">
              <Input placeholder="多个用英文逗号分隔，例如 2260,2920" />
            </Form.Item>
            <Form.Item name="smsbower_except_provider_ids" label="排除供应商 ID（可选）">
              <Input placeholder="多个用英文逗号分隔，例如 2217" />
            </Form.Item>
          </Card>
        )}

        {platform === 'chatgpt' && (
          <Card title="自动上传配置" style={{ marginBottom: 16 }}>
            <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
              注册成功后自动上传到外部管理平台（留空则不上传）
            </Text>

            <Form.Item name="cpa_api_url" label="CPA API URL">
              <Input placeholder="https://your-cpa.example.com" />
            </Form.Item>
            <Form.Item name="cpa_api_key" label="CPA API Key">
              <Input.Password placeholder="Bearer token" />
            </Form.Item>

            <Form.Item name="sub2api_api_url" label="Sub2API API URL">
              <Input placeholder="https://your-sub2api.example.com" />
            </Form.Item>
            <Form.Item name="sub2api_api_key" label="Sub2API API Key">
              <Input.Password placeholder="API Key" />
            </Form.Item>
            <Form.Item name="sub2api_group_ids" label="Sub2API 分组 ID">
              <Input placeholder="多个分组用逗号分隔，例如 2,4,8" />
            </Form.Item>

            <Form.Item name="codex_proxy_url" label="CodexProxy API URL">
              <Input placeholder="https://your-codex-proxy.example.com" />
            </Form.Item>
            <Form.Item name="codex_proxy_key" label="CodexProxy Admin Key">
              <Input.Password placeholder="Admin Key" />
            </Form.Item>
            <Form.Item name="codex_proxy_upload_type" label="CodexProxy 上传类型">
              <Select
                options={[
                  { value: 'at', label: 'AT (Access Token, 推荐)' },
                  { value: 'rt', label: 'RT (Refresh Token)' },
                ]}
              />
            </Form.Item>

            <Form.Item name="team_manager_url" label="Team Manager API URL">
              <Input placeholder="https://your-tm.example.com" />
            </Form.Item>
            <Form.Item name="team_manager_key" label="Team Manager API Key">
              <Input.Password placeholder="API Key" />
            </Form.Item>
          </Card>
        )}

        {captchaSolver === 'yescaptcha' && (
          <Card title="验证码配置" style={{ marginBottom: 16 }}>
            <Form.Item name="yescaptcha_key" label="YesCaptcha Key">
              <Input />
            </Form.Item>
          </Card>
        )}

        {captchaSolver === 'local_solver' && (
          <Card title="本地 Solver 配置" style={{ marginBottom: 16 }}>
            <Form.Item name="solver_url" label="Solver URL">
              <Input />
            </Form.Item>
            <Text type="secondary" style={{ fontSize: 12 }}>
              启动命令: python services/turnstile_solver/start.py --browser_type camoufox --port 8889
            </Text>
          </Card>
        )}

        <Button type="primary" htmlType="submit" block disabled={polling} icon={polling ? <LoadingOutlined /> : <PlayCircleOutlined />}>
          {polling ? '注册中...' : '开始注册'}
        </Button>
      </Form>

      {task && (() => {
        const taskKey = task.id || task.task_id || ''
        const isFinished =
          task.status === 'done' || task.status === 'failed' || task.status === 'stopped'
        return (
          <Card
            title={
              <Space>
                <span>任务状态</span>
                <Tag
                  color={
                    task.status === 'done'
                      ? 'success'
                      : task.status === 'stopped'
                        ? 'warning'
                        : task.status === 'failed'
                          ? 'error'
                          : 'processing'
                  }
                >
                  {task.status || (polling ? 'running' : '')}
                </Tag>
              </Space>
            }
            extra={
              isFinished ? (
                <Button size="small" onClick={clearTask}>
                  关闭面板
                </Button>
              ) : null
            }
            style={{ marginTop: 16 }}
          >
            <Descriptions column={1} size="small">
              <Descriptions.Item label="任务 ID">
                <Text copyable style={{ fontFamily: 'monospace' }}>
                  {taskKey}
                </Text>
              </Descriptions.Item>
              <Descriptions.Item label="进度">{task.progress}</Descriptions.Item>
              <Descriptions.Item label="跳过">{task.skipped ?? 0}</Descriptions.Item>
            </Descriptions>
            {task.success != null && (
              <div style={{ marginTop: 8, color: '#10b981' }}>
                <CheckCircleOutlined /> 成功 {task.success} 个
              </div>
            )}
            {Array.isArray(task.errors) && task.errors.length > 0 && (
              <div style={{ marginTop: 8 }}>
                {task.errors.map((e: string, i: number) => (
                  <div key={i} style={{ color: '#ef4444', marginBottom: 4 }}>
                    <CloseCircleOutlined /> {e}
                  </div>
                ))}
              </div>
            )}
            {task.error && (
              <div style={{ marginTop: 8, color: '#ef4444' }}>
                <CloseCircleOutlined /> {task.error}
              </div>
            )}
            {taskKey ? (
              <div style={{ marginTop: 16 }}>
                <TaskLogPanel
                  taskId={taskKey}
                  taskMeta={{
                    progress: task.progress,
                    total: task.total,
                    started: task.started,
                    completed: task.completed,
                    success: task.success,
                    skipped: task.skipped,
                    errors: task.errors,
                    status: task.status,
                    worker_states: task.worker_states,
                  }}
                />
              </div>
            ) : null}
          </Card>
        )
      })()}
    </div>
  )
}
