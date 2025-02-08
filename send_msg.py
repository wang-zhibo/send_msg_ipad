#!/usr/bin/env python
# -*- coding:utf-8 -*-

# Author : zhibo.wang
# E-mail : gm.zhibo.wang@gmail.com
# Date   :
# Desc   :


import os
import json
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from plugins import *
import plugins
from plugins.send_msg_ipad.file_api import FileWriter
from config import conf
from common.tmp_dir import TmpDir


class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, callback):
        """
        当检测到文件修改时，调用callback函数
        """
        self.callback = callback

    def on_modified(self, event):
        # 只关心 data.json 文件变化
        if event.src_path.endswith('data.json'):
            self.callback()


@plugins.register(
    name="send_msg_ipad",
    desire_priority=669,
    hidden=True,
    desc="watchdog监听文件变化发送消息&微信命令发送消息",
    version="0.0.1",
    author="xdeek"
)
class FileWatcherPlugin(Plugin):
    def __init__(self):
        super().__init__()
        # 设置事件处理函数
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context

        # 启动文件写入的 API 服务
        FileWriter()

        # 文件路径和目录
        curdir = os.path.dirname(__file__)
        self.file_path = os.path.join(curdir, "data.json")
        self.tmp_dir = TmpDir().path()
        self.friend_list_file = os.path.join(self.tmp_dir, "contact_friend.json")
        self.chatroom_list_file = os.path.join(self.tmp_dir, "contact_room.json")

        # 一些状态标记
        self.update_rooms_status = False
        self.update_friends_status = False

        # 创建并启动 watchdog
        self.observer = Observer()
        self.event_handler = FileChangeHandler(self.handle_message)
        self.start_watch()  # 默认启动 watchdog 监听

        # 根据配置获取当前的 channel 类型
        self.channel_type = conf().get("channel_type", "wx")
        self.channel = None
        self.base_url = None
        self.token = None
        self.app_id = None
        self._init_channel()

    def _init_channel(self):
        """
        根据 channel_type 初始化不同的渠道实例
        """
        if self.channel_type == "wx":
            try:
                from lib import itchat
                self.channel = itchat
            except Exception as e:
                logger.error(f"未安装 itchat: {e}")

        elif self.channel_type == "ntchat":
            try:
                from channel.wechatnt.ntchat_channel import wechatnt
                self.channel = wechatnt
            except Exception as e:
                logger.error(f"未安装 ntchat: {e}")

        elif self.channel_type == "gewechat":
            try:
                from lib.gewechat import GewechatClient
                self.base_url = conf().get("gewechat_base_url")
                self.token = conf().get("gewechat_token")
                self.app_id = conf().get("gewechat_app_id")
                self.channel = GewechatClient(self.base_url, self.token)
            except Exception as e:
                logger.error(f"未安装 gewechat: {e}")

        else:
            logger.error(f"不支持的 channel_type: {self.channel_type}")

    def create_reply(self, reply_type, content):
        """
        简化原始 create_reply，将 tag 拼接等逻辑放到外部
        """
        reply = Reply()
        reply.type = reply_type
        reply.content = content
        return reply

    def on_handle_context(self, e_context: EventContext):
        """
        消息处理的主入口，处理各种命令：启动/停止 watchdog、更新群成员/好友列表、手动发送消息...
        """
        if e_context['context'].type != ContextType.TEXT:
            return

        content = e_context['context'].content.strip()

        if content == "$start watchdog":
            self.start_watch()
            e_context.action = EventAction.BREAK_PASS
            e_context['reply'] = self.create_reply(ReplyType.INFO, "watchdog started.")
            # 立即处理文件中的现有数据
            self.handle_message()

        elif content == "$stop watchdog":
            self.stop_watch()
            e_context.action = EventAction.BREAK_PASS
            e_context['reply'] = self.create_reply(ReplyType.INFO, "watchdog stopped.")

        elif content == "$check watchdog":
            reply_text = "watchdog 正在运行,如需停止: $stop watchdog." \
                if self.observer.is_alive() else \
                "watchdog 没在运行,如需启动: $start watchdog."
            e_context['reply'] = self.create_reply(ReplyType.INFO, reply_text)
            e_context.action = EventAction.BREAK_PASS

        elif content == "更新群成员":
            # 仅在 channel_type = "gewechat" 时有效
            if self.channel_type == "gewechat" and not self.update_rooms_status:
                self.update_rooms_status = True
                msg = self.update_rooms("更新群成员")
                e_context["reply"] = self.create_reply(ReplyType.TEXT, msg)
                self.update_rooms_status = False
            e_context.action = EventAction.BREAK_PASS

        elif content == "更新好友列表":
            # 同上，只有 gewechat 时有效
            if self.channel_type == "gewechat" and not self.update_friends_status:
                self.update_friends_status = True
                msg = self.update_friends("更新好友列表")
                e_context["reply"] = self.create_reply(ReplyType.TEXT, msg)
                self.update_friends_status = False
            e_context.action = EventAction.BREAK_PASS

        elif content.startswith("$send_msg"):
            self._handle_send_msg_command(e_context, content)
            e_context.action = EventAction.BREAK_PASS

    def _handle_send_msg_command(self, e_context, content):
        """
        专门处理 $send_msg 命令的逻辑，例如：
        $send_msg [备注名1, 备注名2] 消息内容 group[群聊1, 群聊2]
        $send_msg [所有人] 消息内容 group[群聊名称]
        """
        try:
            # 提取接收者名称
            receiver_names = []
            receiver_start = content.find('[')
            receiver_end = content.find(']', receiver_start)
            if receiver_start != -1 and receiver_end != -1:
                receiver_names = [
                    name.strip() for name in content[receiver_start + 1:receiver_end].split(',')
                ]
            # 截掉“[xx, xx]”这部分后的消息
            msg_after_receivers = content[receiver_end + 1:].strip() if receiver_end != -1 else ""

            # 提取群聊名称
            group_names = []
            group_start = msg_after_receivers.find('group[')
            if group_start != -1:
                group_end = msg_after_receivers.find(']', group_start)
                if group_end != -1:
                    group_names = [
                        name.strip()
                        for name in msg_after_receivers[group_start + 6 : group_end].split(',')
                    ]
                    # 再截断群聊信息
                    msg_after_receivers = msg_after_receivers[:group_start].strip()

            # 判断是否@所有人
            if "所有人" in receiver_names or "all" in receiver_names:
                receiver_names = ["所有人"]

            # 剩余的部分即要发送的具体消息内容
            msg_to_send = msg_after_receivers.strip()
            self.send_message(receiver_names, msg_to_send, group_names)

            e_context['reply'] = self.create_reply(ReplyType.INFO, "消息发送成功.")
        except Exception as e:
            logger.error(f"消息发送失败: {e}")
            e_context['reply'] = self.create_reply(ReplyType.ERROR, f"消息发送失败: {str(e)}")

    def start_watch(self):
        """
        启动 watchdog
        """
        if not self.observer.is_alive():
            self.observer = Observer()
            watch_dir = os.path.dirname(self.file_path)
            self.observer.schedule(self.event_handler, path=watch_dir, recursive=False)
            self.observer.start()
            logger.info("watchdog started.")
        else:
            logger.info("watchdog is already running.")

    def stop_watch(self):
        """
        停止 watchdog
        """
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            logger.info("watchdog stopped.")
        else:
            logger.info("watchdog is not running.")

    def handle_message(self):
        """
        当 data.json 文件变化时，读取内容并发送消息
        """
        try:
            with open(self.file_path, 'r', encoding='utf-8') as file:
                data_str = file.read().strip()
                if not data_str:
                    return
                data_list = json.loads(data_str)

            # 批量发送完后，清空文件
            for record in data_list:
                self.process_message(record)
            with open(self.file_path, 'w', encoding='utf-8') as file:
                file.write('')

        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"读取文件 {self.file_path} 出错: {e}")

    def process_message(self, data):
        """
        读取 data.json 中的一条数据并发送
        """
        try:
            receiver_names = data["receiver_name"]  # 可能是字符串或者列表
            content = data["message"]
            group_names = data["group_name"]  # 同样可能是字符串或者列表
            self.send_message(receiver_names, content, group_names)
        except Exception as e:
            logger.error(f"处理消息时发生异常: {e}")

    def send_message(self, receiver_names, content, group_names=None):
        """
        发送消息，根据 channel_type 选择使用 itchat, ntchat 或 gewechat
        :param receiver_names: list[str] 接收者名称列表（可能包含 "所有人"）
        :param content: str 消息内容
        :param group_names: list[str] 群聊名称列表
        """
        # 保证都是 list
        if isinstance(receiver_names, str):
            receiver_names = [receiver_names]
        if isinstance(group_names, str):
            group_names = [group_names]

        group_names = group_names or []

        if not self.channel:
            logger.error("channel 未初始化, 无法发送消息")
            return

        try:
            if self.channel_type == "wx":
                self._send_itchat_message(receiver_names, content, group_names)
            elif self.channel_type == "ntchat":
                self._send_ntchat_message(receiver_names, content, group_names)
            elif self.channel_type == "gewechat":
                self._send_gewechat_message(receiver_names, content, group_names)
            else:
                raise ValueError(f"不支持的 channel_type: {self.channel_type}")
        except Exception as e:
            logger.error(f"发送消息时发生异常: {e}")
            raise e

    ############################
    #     GEWECHAT 相关逻辑    #
    ############################
    def _send_gewechat_message(self, receiver_names, content, group_names, type_=False):
        """
        给 gewechat 发送消息，目前没有直接支持一条消息内 @ 多个人，只能拆分发送。
        :param type_: 当 False 时，先发一条正常消息 在分别艾特。True 艾特和消息一起发送。
        """
        # 如果有群
        if group_names:
            self._send_gewechat_group_message(receiver_names, content, group_names, type_)
        else:
            # 否则发给好友
            self._send_gewechat_friend_message(receiver_names, content)

    def _send_gewechat_group_message(self, receiver_names, content, group_names, type_):
        # 读取本地缓存的群通讯录
        if not os.path.exists(self.chatroom_list_file):
            logger.info("群通讯录文件不存在，请先执行“更新群成员”")
            return

        chatroom_list = json.load(open(self.chatroom_list_file, 'r', encoding='utf-8'))
        # 匹配群聊
        chatroom_infos = []
        for group_name in group_names:
            for chatroom_info in chatroom_list:
                if group_name == chatroom_info.get("nickName"):
                    chatroom_infos.append(chatroom_info)
                    break

        if not chatroom_infos:
            logger.info("没有找到对应的群聊")
            return

        # 针对每个群聊发送消息
        for chatroom_info in chatroom_infos:
            to_room_id = chatroom_info.get("chatroomId")
            chatroom_name = chatroom_info.get("nickName")
            if not receiver_names or receiver_names == ["所有人"]:
                # @所有人
                content_at = "@所有人 " if receiver_names == ["所有人"] else ""
                final_content = f"{content_at}{content}"
                self.channel.post_text(self.app_id, to_room_id, final_content, ats="")
            else:
                members = chatroom_info.get("memberList", [])
                if len(receiver_names) > 1:
                    logger.info("多人消息")
                    # 定向 @ 群成员
                    # 先发一条纯文本
                    if not type_:
                        self.channel.post_text(self.app_id, to_room_id, content, ats="")

                    for receiver_name in receiver_names:
                        for mem in members:
                            if mem.get("nickName") == receiver_name or mem.get("displayName") == receiver_name:
                                wxid = mem.get("wxid")
                                content_at = f"@{receiver_name} " if receiver_name else ""
                                if not type_:
                                    logger.info(f"手动发送微信群聊消息成功, 发送群聊:{chatroom_name}, 接收者:{receiver_name}, 消息内容：{content}")
                                    logger.info(f"to_room_id: {to_room_id}, content: {content}, ats: {wxid}")
                                    end_content = f'{content_at} '
                                    self.channel.post_text(self.app_id, to_room_id, content_at, ats=wxid)
                                else:
                                    combined = f"{content_at}{content}"
                                    self.channel.post_text(self.app_id, to_room_id, combined, ats=wxid)
                else:
                    logger.info("单人消息")
                    receiver_name = receiver_names[0]
                    for mem in members:
                        if mem.get("nickName") == receiver_name or mem.get("displayName") == receiver_name:
                            wxid = mem.get("wxid")
                            content_at = f"@{receiver_name} " if receiver_name else ""
                            combined = f"{content_at}{content}"
                            logger.info(f"手动发送微信群聊消息成功, 发送群聊:{chatroom_name}, 接收者:{receiver_name}, 消息内容：{content}")
                            self.channel.post_text(self.app_id, to_room_id, combined, ats=wxid)

    def _send_gewechat_friend_message(self, receiver_names, content):
        if not os.path.exists(self.friend_list_file):
            logger.info("好友通讯录文件不存在，请先执行“更新好友列表”")
            return

        friend_list = json.load(open(self.friend_list_file, 'r', encoding='utf-8'))
        # 匹配好友
        friend_infos = []
        for receiver_name in receiver_names:
            for friend_info in friend_list:
                if receiver_name in (friend_info.get("nickName"), friend_info.get("remark")):
                    friend_infos.append(friend_info)
                    break

        if not friend_infos:
            logger.info("未找到对应好友")
            return

        for friend_info in friend_infos:
            to_friend_id = friend_info.get("userName")
            nickName = friend_info.get("nickName")
            logger.info(f"手动发送微信消息成功, 发送人:{nickName} 消息内容：{content}")
            self.channel.post_text(self.app_id, to_friend_id, content, ats="")

    def update_rooms(self, tag):
        """
        更新群列表
        """
        msg = f"{tag}: 服务器睡着了,请稍后再试"
        try:
            room_counts = self.load_contact_rooms()
            msg = f"{tag}: 成功" if room_counts > 0 else f"{tag}: 失败"
        except Exception as e:
            logger.error(f"{tag}: 服务器内部错误 {e}")
        return msg

    def load_contact_rooms(self):
        """
        从服务器获取群聊列表并保存到本地
        """
        chatroom_list = []
        contacts_json = self.channel.fetch_contacts_list(self.app_id)
        if contacts_json.get("ret") == 200:
            chatrooms = contacts_json["data"].get("chatrooms", [])
            for chatroom_id in chatrooms:
                info_json = self.channel.get_chatroom_info(self.app_id, chatroom_id)
                if info_json.get("ret") == 200:
                    room_info = info_json["data"]
                    if room_info.get("nickName"):  # 为空时可能是没修改初始群名或未保存到通讯录
                        chatroom_list.append(room_info)
        self.save_contact_rooms(chatroom_list)
        return len(chatroom_list)

    def save_contact_rooms(self, chatroom_list):
        json.dump(chatroom_list, open(self.chatroom_list_file, 'w', encoding='utf-8'), ensure_ascii=False)
        logger.info("保存rooms结束")

    def update_friends(self, tag):
        """
        更新好友列表
        """
        msg = f"{tag}: 服务器睡着了,请稍后再试"
        try:
            friends_counts = self.load_contact_friends()
            msg = f"{tag}: 成功" if friends_counts > 0 else f"{tag}: 失败"
        except Exception as e:
            logger.error(f"{tag}: 服务器内部错误 {e}")
        return msg

    def load_contact_friends(self):
        friend_list = []
        contacts_json = self.channel.fetch_contacts_list(self.app_id)
        if contacts_json.get("ret") == 200:
            friends = contacts_json["data"].get("friends", [])
            for friend_id in friends:
                info_json = self.channel.get_detail_info(self.app_id, [friend_id])
                if info_json.get("ret") == 200:
                    data_list = info_json.get("data", [])
                    if data_list:
                        friend_info = data_list[0]
                        if friend_info.get("nickName"):
                            friend_list.append(friend_info)
        self.save_contact_friends(friend_list)
        return len(friend_list)

    def save_contact_friends(self, friend_list):
        json.dump(friend_list, open(self.friend_list_file, 'w', encoding='utf-8'), ensure_ascii=False)
        logger.info("保存friend结束")

    ############################
    #      ITCHAT 相关逻辑     #
    ############################
    def _send_itchat_message(self, receiver_names, content, group_names):
        """
        使用 itchat 发送消息
        """
        # 更新缓存
        self.channel.get_friends(update=True)
        self.channel.get_chatrooms(update=True)

        media_type = self._detect_media_type(content)

        # 发送到群聊
        if group_names:
            for group_name in group_names:
                chatrooms = self.channel.search_chatrooms(name=group_name)
                if not chatrooms:
                    raise ValueError(f"没有找到对应的群聊: {group_name}")
                chatroom = chatrooms[0]

                if receiver_names:
                    for receiver_name in receiver_names:
                        at_content = ""
                        if receiver_name == "所有人":
                            at_content = "@所有人 "
                        else:
                            at_content = self._find_member_in_itchat_chatroom(chatroom, receiver_name)
                        self.send_msg(media_type, content, chatroom.UserName, at_content)
                        logger.info(f"手动发送微信群聊消息成功, 群:{group_name}, 接收者:{receiver_name}, 内容:{content}")
                else:
                    # 无接收者名单时只发送文本
                    self.send_msg(media_type, content, chatroom.UserName)
                    logger.info(f"手动发送微信群聊消息成功, 群:{group_name}, 内容:{content}")
        else:
            # 发送给好友
            for receiver_name in receiver_names:
                friends = self.channel.search_friends(remarkName=receiver_name) or \
                          self.channel.search_friends(name=receiver_name)
                if not friends:
                    raise ValueError(f"没有找到对应的好友: {receiver_name}")
                friend = friends[0]
                self.send_msg(media_type, content, friend.UserName)
                logger.info(f"手动发送微信消息成功, 发送人:{friend.NickName}, 消息:{content}")

    def _find_member_in_itchat_chatroom(self, chatroom, member_name):
        """
        在 chatroom 中寻找指定成员，返回用于 @ 的字符串
        """
        for member in chatroom.MemberList:
            if member.NickName == member_name or member.DisplayName == member_name:
                return f"@{member.NickName} "
        # 如果没找到，就再去好友列表里找一下
        friend = self.channel.search_friends(remarkName=member_name) or \
                 self.channel.search_friends(name=member_name)
        if friend:
            return f"@{friend[0].NickName} "
        raise ValueError(f"在群聊[{chatroom.NickName}]中没有找到指定成员: {member_name}")

    ############################
    #      NTCHAT 相关逻辑     #
    ############################
    def _send_ntchat_message(self, receiver_names, content, group_names):
        media_type = self._detect_media_type(content)

        # 群聊逻辑
        if group_names:
            rooms = self.channel.get_rooms()  # 获取所有群聊
            for group_name in group_names:
                match_room = None
                for room in rooms:
                    if room.get("nickname") == group_name:
                        match_room = room
                        break
                if not match_room:
                    logger.warning(f"未找到对应的群聊: {group_name}")
                    continue

                wxid = match_room["wxid"]
                # 如果没有要 @ 的人，则直接发普通文本
                if not receiver_names or receiver_names == ["所有人"]:
                    at_content = "@所有人 " if receiver_names == ["所有人"] else ""
                    final_msg = f"{at_content}{content}"
                    self.channel.send_room_at_msg(wxid, final_msg, [])
                    logger.info(f"成功发送到群聊[{group_name}], 内容: {final_msg}")
                    continue

                # 否则在群成员中搜索
                room_members = self.channel.get_room_members(wxid).get("member_list", [])
                member_wxids = []
                for member_name in receiver_names:
                    for mem in room_members:
                        if mem.get("nickname") == member_name:
                            member_wxids.append(mem["wxid"])
                            break
                if len(member_wxids) == len(receiver_names):
                    # 构造 @ 内容
                    at_str = " ".join([f"@{m}" for m in receiver_names])
                    final_msg = f"{at_str} {content}"
                    self.channel.send_room_at_msg(wxid, final_msg, member_wxids)
                    logger.info(f"成功发送群聊[{group_name}] at_msg: {final_msg}")
                else:
                    logger.warning(f"未在群[{group_name}]找到全部指定成员: {receiver_names}")
        else:
            # 单聊逻辑
            for receiver_name in receiver_names:
                wxid = self._find_friend_by_name(receiver_name)
                if not wxid:
                    raise ValueError(f"没有找到对应的好友: {receiver_name}")
                self._send_ntchat_media_or_text(media_type, content, wxid)
                logger.info(f"成功发送单聊消息给 [{receiver_name}], 内容: {content}")

    def _find_friend_by_name(self, friend_name):
        """
        根据好友名称查找wxid
        """
        friends = self.channel.get_contacts()  # 获取所有好友
        for friend in friends:
            if friend["nickname"] == friend_name or friend["remark"] == friend_name:
                return friend["wxid"]
        return None

    def _send_ntchat_media_or_text(self, media_type, content, wxid):
        """
        ntchat 根据消息类型发送文本、图片、视频或文件
        """
        if media_type == "text":
            self.channel.send_text(wxid, content)
        else:
            local_file = self._download_file(content)
            if not local_file:
                raise ValueError(f"无法下载文件：{content}")

            if media_type == "img":
                self.channel.send_image(wxid, local_file)
            elif media_type == "video":
                self.channel.send_video(wxid, local_file)
            elif media_type == "file":
                self.channel.send_file(wxid, local_file)
            else:
                logger.error(f"不支持的消息类型: {media_type}")
            os.remove(local_file)  # 发送完成后删除临时文件

    ############################
    #    ITCHAT 发送辅助逻辑    #
    ############################
    def send_msg(self, msg_type, content, to_user_name, at_content=None):
        """
        统一给 itchat 使用的发送函数
        """
        if msg_type == 'text':
            msg_to_send = f"{at_content}{content}" if at_content else content
            self.channel.send(msg_to_send, to_user_name)
        else:
            # 文件类消息
            local_file_path = self._download_file(content)
            if not local_file_path:
                raise ValueError(f"无法下载文件: {content}")

            # 先发 @ 内容
            if at_content:
                self.channel.send(at_content, to_user_name)

            if msg_type == 'img':
                self.channel.send_image(local_file_path, to_user_name)
            elif msg_type == 'video':
                self.channel.send_video(local_file_path, to_user_name)
            elif msg_type == 'file':
                self.channel.send_file(local_file_path, to_user_name)
            else:
                raise ValueError(f"不支持的消息类型: {msg_type}")

            os.remove(local_file_path)

    ############################
    #        工具函数          #
    ############################
    def _detect_media_type(self, content):
        """
        检测消息内容中是否包含 URL 并判断其文件类型(仅简单判断后缀)
        """
        # 如果是纯文本
        if not (content.startswith("http://") or content.startswith("https://")):
            return "text"

        # 根据后缀猜测媒体类型
        lower_content = content.lower()
        if lower_content.endswith((".jpg", ".jpeg", ".png", ".gif", ".img")):
            return "img"
        elif lower_content.endswith((".mp4", ".avi", ".mov", ".pdf")):
            return "video"
        elif lower_content.endswith((".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".txt", ".csv")):
            return "file"
        else:
            logger.warning(f"无法识别的文件后缀: {content}")
            return "text"

    def _download_file(self, url):
        """
        下载文件到当前工作目录下
        """
        try:
            response = requests.get(url)
            if response.status_code == 200:
                file_name = os.path.basename(url)
                with open(file_name, 'wb') as f:
                    f.write(response.content)
                return file_name
            return None
        except Exception as e:
            logger.error(f"下载文件时发生异常: {e}")
            return None

    def get_help_text(self, **kwargs):
        return (
            "1. watchdog监听文件变化插件, 监听data.json文件变化并发送微信通知 (默认已启动)\n"
            "   - 启动监听: $start watchdog\n"
            "   - 停止监听: $stop watchdog\n"
            "   - 查看监听状态: $check watchdog\n\n"
            "2. 微信命令发送消息:\n"
            "   - $send_msg [微信备注名1,微信备注名2] 消息内容\n"
            "   - $send_msg [微信备注名1,微信备注名2] 消息内容 group[群聊名称1,群聊名称2]\n"
            "   - $send_msg [所有人] 消息内容 group[群聊名称1,群聊名称2]\n"
            "3. 更新联系人:\n"
            "   - 更新群成员\n"
            "   - 更新好友列表\n"
        )

