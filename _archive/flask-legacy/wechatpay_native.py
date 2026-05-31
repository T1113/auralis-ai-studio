"""微信支付 Native 扫码支付封装。

使用社区维护的 wechatpayv3 SDK。生产环境必须在 .env 配齐：
- WECHATPAY_MCH_ID            商户号
- WECHATPAY_SERIAL_NO         商户证书序列号
- WECHATPAY_PRIVATE_KEY_PATH  apiclient_key.pem 文件路径（推荐）
  或 WECHATPAY_PRIVATE_KEY     直接贴 PEM 文本（不建议）
- WECHATPAY_API_V3_KEY        APIv3 密钥（用于解密回调）
- WECHATPAY_APPID             绑定的公众号/小程序/应用 APPID
- WECHATPAY_NOTIFY_URL        支付成功回调 URL（公网 HTTPS）

调用流程：
1. ``WeChatPayClient.is_configured()`` 检查环境是否齐全
2. ``client.create_native_order(out_trade_no, amount_cents, description)`` 创建订单 → 返回二维码 URL
3. 微信回调 ``/api/payments/wechat/notify`` → ``client.parse_notification(headers, body)`` 解密验签
4. ``client.query_order(out_trade_no)`` 主动查单兜底
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 延迟导入：未配置时不强制安装 wechatpayv3
try:
    from wechatpayv3 import WeChatPay, WeChatPayType
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    WeChatPay = None  # type: ignore[assignment]
    WeChatPayType = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False


REQUIRED_ENV_VARS = (
    'WECHATPAY_MCH_ID',
    'WECHATPAY_SERIAL_NO',
    'WECHATPAY_API_V3_KEY',
    'WECHATPAY_APPID',
    'WECHATPAY_NOTIFY_URL',
)


@dataclass(frozen=True)
class NativeOrder:
    out_trade_no: str
    code_url: str
    prepay_id: Optional[str] = None


@dataclass(frozen=True)
class PaymentNotification:
    out_trade_no: str
    transaction_id: str
    trade_state: str        # SUCCESS / REFUND / NOTPAY / CLOSED / REVOKED / USERPAYING / PAYERROR
    amount_total_cents: int
    payer_openid: Optional[str]
    raw_resource: dict


class WeChatPayConfigError(RuntimeError):
    """配置缺失或无效。"""


class WeChatPayClient:
    """微信支付 Native 扫码客户端，全局单例。"""

    _instance: 'WeChatPayClient | None' = None

    def __init__(self) -> None:
        if not _SDK_AVAILABLE:
            raise WeChatPayConfigError(
                'wechatpayv3 未安装。请在 requirements.txt 加上 wechatpayv3>=1.3.0 并重装依赖。'
            )

        missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
        if missing:
            raise WeChatPayConfigError(f'缺少环境变量：{", ".join(missing)}')

        private_key = self._load_private_key()
        cert_dir = Path(os.getenv('WECHATPAY_CERT_DIR', './wechatpay_certs')).resolve()
        cert_dir.mkdir(parents=True, exist_ok=True)

        self.appid: str = os.environ['WECHATPAY_APPID']
        self.notify_url: str = os.environ['WECHATPAY_NOTIFY_URL']

        self._client = WeChatPay(
            wechatpay_type=WeChatPayType.NATIVE,
            mchid=os.environ['WECHATPAY_MCH_ID'],
            private_key=private_key,
            cert_serial_no=os.environ['WECHATPAY_SERIAL_NO'],
            apiv3_key=os.environ['WECHATPAY_API_V3_KEY'],
            appid=self.appid,
            notify_url=self.notify_url,
            cert_dir=str(cert_dir),
        )

    @staticmethod
    def _load_private_key() -> str:
        key_path = os.getenv('WECHATPAY_PRIVATE_KEY_PATH')
        if key_path:
            path = Path(key_path).expanduser().resolve()
            if not path.exists():
                raise WeChatPayConfigError(f'WECHATPAY_PRIVATE_KEY_PATH 指向的文件不存在：{path}')
            return path.read_text(encoding='utf-8')
        inline = os.getenv('WECHATPAY_PRIVATE_KEY')
        if inline:
            return inline.replace('\\n', '\n')
        raise WeChatPayConfigError(
            '需要配置 WECHATPAY_PRIVATE_KEY_PATH 或 WECHATPAY_PRIVATE_KEY 之一。'
        )

    @classmethod
    def is_configured(cls) -> bool:
        if not _SDK_AVAILABLE:
            return False
        return all(os.getenv(var) for var in REQUIRED_ENV_VARS) and bool(
            os.getenv('WECHATPAY_PRIVATE_KEY_PATH') or os.getenv('WECHATPAY_PRIVATE_KEY')
        )

    @classmethod
    def get_instance(cls) -> 'WeChatPayClient':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def create_native_order(
        self,
        out_trade_no: str,
        amount_cents: int,
        description: str,
    ) -> NativeOrder:
        """创建 Native 扫码订单，返回二维码 code_url。"""
        if amount_cents <= 0:
            raise ValueError('amount_cents 必须为正整数（单位：分）')

        code, message = self._client.pay(
            description=description[:127],  # 微信限制 127 字符
            out_trade_no=out_trade_no,
            amount={'total': int(amount_cents), 'currency': 'CNY'},
            pay_type=WeChatPayType.NATIVE,
        )
        if code != 200:
            raise RuntimeError(f'WeChatPay native 下单失败：HTTP {code} / {message}')
        try:
            payload = json.loads(message) if isinstance(message, str) else message
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'WeChatPay 返回的 message 不是 JSON：{message!r}') from exc
        code_url = payload.get('code_url')
        if not code_url:
            raise RuntimeError(f'WeChatPay 返回缺少 code_url：{payload}')
        return NativeOrder(out_trade_no=out_trade_no, code_url=code_url, prepay_id=payload.get('prepay_id'))

    def query_order(self, out_trade_no: str) -> Optional[PaymentNotification]:
        """主动查单（兜底，防止回调丢失）。"""
        code, message = self._client.query(out_trade_no=out_trade_no)
        if code != 200:
            return None
        try:
            data = json.loads(message) if isinstance(message, str) else message
        except json.JSONDecodeError:
            return None
        return PaymentNotification(
            out_trade_no=data.get('out_trade_no', out_trade_no),
            transaction_id=data.get('transaction_id', ''),
            trade_state=data.get('trade_state', ''),
            amount_total_cents=int((data.get('amount') or {}).get('total', 0)),
            payer_openid=(data.get('payer') or {}).get('openid'),
            raw_resource=data,
        )

    def parse_notification(self, headers: dict, body: bytes) -> PaymentNotification:
        """解密 + 验签微信回调。SDK 会做完整校验，失败抛异常。"""
        result = self._client.callback(headers=headers, body=body)
        if not result or result.get('event_type') not in {'TRANSACTION.SUCCESS'}:
            raise RuntimeError(f'回调事件异常：{result}')
        resource = result.get('resource') or {}
        return PaymentNotification(
            out_trade_no=resource.get('out_trade_no', ''),
            transaction_id=resource.get('transaction_id', ''),
            trade_state=resource.get('trade_state', ''),
            amount_total_cents=int((resource.get('amount') or {}).get('total', 0)),
            payer_openid=(resource.get('payer') or {}).get('openid'),
            raw_resource=resource,
        )
