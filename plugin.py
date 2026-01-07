import sqlite3
import random
import time
import json
from pathlib import Path
from typing import Optional, Type, Tuple, List, Union
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from src.plugin_system import (
    BaseCommand,
    BasePlugin,
    register_plugin,
    ConfigField,
    ComponentInfo,
    chat_api,
    get_logger
)

logger = get_logger("drift-bottle-plugin")


class BottleDatabase:
    """漂流瓶数据库管理类"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bottles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    status INTEGER NOT NULL DEFAULT 0,
                    sender INTEGER NOT NULL,
                    sender_group INTEGER NOT NULL,
                    picker INTEGER,
                    picker_group INTEGER,
                    created_at INTEGER,
                    picked_at INTEGER
                )
            ''')
            # 添加索引优化查询
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_bottles_status 
                ON bottles(status)
            ''')
            conn.commit()

    def save_bottle(self, content: str, sender: str, sender_group: str) -> int:
        """保存新漂流瓶

        Args:
            content: 漂流瓶内容
            sender: 发送者QQ号
            sender_group: 发送者群号

        Returns:
            新漂流瓶的ID
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO bottles (content, status, sender, sender_group, created_at) 
                   VALUES (?, 0, ?, ?, ?)''',
                (content, int(sender), int(sender_group), int(time.time()))
            )
            conn.commit()
            return cursor.lastrowid

    def get_random_bottle(self) -> Optional[dict]:
        """随机获取一个未被捡起的漂流瓶

        Returns:
            漂流瓶信息字典，如果没有可用漂流瓶则返回None
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, content, sender, sender_group, created_at FROM bottles WHERE status = 0'
            )
            results = cursor.fetchall()
            if not results:
                return None

            bottle = random.choice(results)
            return {
                'id': bottle[0],
                'content': bottle[1],
                'sender': str(bottle[2]),
                'sender_group': str(bottle[3]),
                'created_at': bottle[4]
            }

    def pick_bottle(self, bottle_id: int, picker: str, picker_group: str) -> bool:
        """更新漂流瓶状态为已被捡起

        Args:
            bottle_id: 漂流瓶ID
            picker: 拾取者QQ号
            picker_group: 拾取者群号

        Returns:
            是否更新成功
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''UPDATE bottles SET status = 1, picker = ?, picker_group = ?, picked_at = ? 
                   WHERE id = ? AND status = 0''',
                (int(picker), int(picker_group), int(time.time()), bottle_id)
            )
            conn.commit()
            return cursor.rowcount > 0


class ThrowBottleCommand(BaseCommand):
    """扔漂流瓶命令"""
    command_name = "throw-bottle"
    command_description = "扔一个漂流瓶到大海中"
    command_pattern = r'^扔漂流瓶.+$'

    @staticmethod
    def _make_request(url: str, payload: dict) -> Tuple[bool, Union[dict, str]]:
        """发送HTTP POST请求到napcat"""
        try:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            request = Request(
                url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
                return True, result
        except HTTPError as e:
            return False, f"HTTP错误: {e.code}"
        except URLError as e:
            return False, f"网络错误: {e.reason}"
        except json.JSONDecodeError as e:
            return False, f"JSON解析错误: {e}"
        except Exception as e:
            return False, f"请求错误: {str(e)}"

    def get_stranger_info(self, address: str, port: int, user_id: str) -> Tuple[bool, Union[dict, str]]:
        """获取用户信息（不需要在同一群组）"""
        url = f"http://{address}:{port}/get_stranger_info"
        payload = {"user_id": user_id}

        success, result = self._make_request(url, payload)
        if not success:
            return False, result

        data = result.get("data")
        if data is None:
            return False, "获取用户信息失败：返回数据为空"
        return True, data

    def get_group_info(self, address: str, port: int, group_id: str) -> Tuple[bool, Union[dict, str]]:
        """获取群信息"""
        url = f"http://{address}:{port}/get_group_info"
        payload = {"group_id": group_id, "no_cache": False}

        success, result = self._make_request(url, payload)
        if not success:
            return False, result

        data = result.get("data")
        if data is None:
            return False, "获取群信息失败：返回数据为空"
        return True, data

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        # 获取消息内容
        message_text = self.message.processed_plain_text.strip()

        # 提取漂流瓶内容（去掉"扔漂流瓶"前缀）
        content = message_text[4:].strip()  # "扔漂流瓶"是4个字符

        if not content:
            await self.send_text("漂流瓶内容不能为空哦~")
            return False, "内容为空", 1

        # 获取用户信息
        user_info = self.message.message_info.user_info if self.message.message_info else None
        if not user_info:
            await self.send_text("无法获取用户信息")
            return False, "无法获取用户信息", 1

        user_id = str(user_info.user_id)

        # 获取群组信息
        chat_stream = self.message.chat_stream
        stream_type = chat_api.get_stream_type(chat_stream)

        if stream_type != "group":
            await self.send_text("漂流瓶只能在群聊中使用哦~")
            return False, "非群聊环境", 1

        group_id = str(chat_stream.group_info.group_id)

        # 获取napcat配置
        napcat_address = self.get_config("napcat.address", "napcat")
        napcat_port = self.get_config("napcat.port", 3000)

        # 获取用户昵称
        user_name = "未知"
        success, stranger_info = self.get_stranger_info(napcat_address, napcat_port, user_id)
        if success:
            user_name = stranger_info.get("nickname") or stranger_info.get("nick", "未知")

        # 获取群名称
        group_name = "未知"
        success, group_info = self.get_group_info(napcat_address, napcat_port, group_id)
        if success:
            group_name = group_info.get("group_name", "未知")

        # 初始化数据库
        current_dir = Path(__file__).parent.absolute()
        db_path = current_dir / "bottles.db"
        db = BottleDatabase(db_path)

        # 保存漂流瓶
        bottle_id = db.save_bottle(content, user_id, group_id)

        logger.info(f"用户 {user_name}({user_id}) 在群 {group_name}({group_id}) 扔了一个漂流瓶(ID:{bottle_id}): {content[:20]}...")

        # 发送确认消息
        response = f"你将一个写着【{content}】的纸条塞入瓶中扔进大海，希望有人捞到吧~"
        await self.send_text(response)

        return True, "扔漂流瓶成功", 1


class PickBottleCommand(BaseCommand):
    """捡漂流瓶命令"""
    command_name = "pick-bottle"
    command_description = "从大海中捡一个漂流瓶"
    command_pattern = r'^捡漂流瓶$'

    @staticmethod
    def _make_request(url: str, payload: dict) -> Tuple[bool, Union[dict, str]]:
        """发送HTTP POST请求到napcat"""
        try:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            request = Request(
                url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
                return True, result
        except HTTPError as e:
            return False, f"HTTP错误: {e.code}"
        except URLError as e:
            return False, f"网络错误: {e.reason}"
        except json.JSONDecodeError as e:
            return False, f"JSON解析错误: {e}"
        except Exception as e:
            return False, f"请求错误: {str(e)}"

    def get_stranger_info(self, address: str, port: int, user_id: str) -> Tuple[bool, Union[dict, str]]:
        """获取用户信息（不需要在同一群组）"""
        url = f"http://{address}:{port}/get_stranger_info"
        payload = {"user_id": user_id}

        success, result = self._make_request(url, payload)
        if not success:
            return False, result

        data = result.get("data")
        if data is None:
            return False, "获取用户信息失败：返回数据为空"
        return True, data

    def get_group_info(self, address: str, port: int, group_id: str) -> Tuple[bool, Union[dict, str]]:
        """获取群信息"""
        url = f"http://{address}:{port}/get_group_info"
        payload = {"group_id": group_id, "no_cache": False}

        success, result = self._make_request(url, payload)
        if not success:
            return False, result

        data = result.get("data")
        if data is None:
            return False, "获取群信息失败：返回数据为空"
        return True, data

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        # 获取用户信息
        user_info = self.message.message_info.user_info if self.message.message_info else None
        if not user_info:
            await self.send_text("无法获取用户信息")
            return False, "无法获取用户信息", 1

        user_id = str(user_info.user_id)

        # 获取群组信息
        chat_stream = self.message.chat_stream
        stream_type = chat_api.get_stream_type(chat_stream)

        if stream_type != "group":
            await self.send_text("漂流瓶只能在群聊中使用哦~")
            return False, "非群聊环境", 1

        group_id = str(chat_stream.group_info.group_id)

        # 获取napcat配置
        napcat_address = self.get_config("napcat.address", "napcat")
        napcat_port = self.get_config("napcat.port", 3000)

        # 获取当前用户昵称
        current_user_name = "未知"
        success, current_stranger_info = self.get_stranger_info(napcat_address, napcat_port, user_id)
        if success:
            current_user_name = current_stranger_info.get("nickname") or current_stranger_info.get("nick", "未知")

        # 获取当前群名称
        current_group_name = "未知"
        success, current_group_info = self.get_group_info(napcat_address, napcat_port, group_id)
        if success:
            current_group_name = current_group_info.get("group_name", "未知")

        # 初始化数据库
        current_dir = Path(__file__).parent.absolute()
        db_path = current_dir / "bottles.db"
        db = BottleDatabase(db_path)

        # 随机获取一个漂流瓶
        bottle = db.get_random_bottle()

        if not bottle:
            await self.send_text("大海里暂时没有漂流瓶，试试自己扔一个吧~")
            return True, "没有可用漂流瓶", 1

        # 获取发送者昵称
        sender_name = "未知"
        success, stranger_info = self.get_stranger_info(napcat_address, napcat_port, bottle['sender'])
        if success:
            sender_name = stranger_info.get("nickname") or stranger_info.get("nick", "未知")

        # 获取发送者群名称
        sender_group_name = "未知"
        success, group_info = self.get_group_info(napcat_address, napcat_port, bottle['sender_group'])
        if success:
            sender_group_name = group_info.get("group_name", "未知")

        # 更新漂流瓶状态
        db.pick_bottle(bottle['id'], user_id, group_id)

        logger.info(f"用户 {current_user_name}({user_id}) 在群 {current_group_name}({group_id}) 捡到了漂流瓶(ID:{bottle['id']})")

        # 构建返回消息
        response = f"""你在海边捡到了一个漂流瓶，瓶中的纸条上写着：
{bottle['content']}
BY：{sender_name} ({bottle['sender']})
From：{sender_group_name} ({bottle['sender_group']})"""

        await self.send_text(response)

        return True, "捡漂流瓶成功", 1


@register_plugin
class DriftBottlePlugin(BasePlugin):
    """漂流瓶插件"""
    plugin_name = "drift-bottle-plugin"
    enable_plugin = True
    dependencies = []
    python_dependencies = []
    config_file_name = "config.toml"
    config_section_descriptions = {
        "plugin": "插件基础配置",
        "napcat": "napcat服务器配置"
    }
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.0.0", description="配置版本")
        },
        "napcat": {
            "address": ConfigField(type=str, default="napcat", description="napcat服务器连接地址"),
            "port": ConfigField(type=int, default=3000, description="napcat服务器端口")
        }
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (ThrowBottleCommand.get_command_info(), ThrowBottleCommand),
            (PickBottleCommand.get_command_info(), PickBottleCommand)
        ]
