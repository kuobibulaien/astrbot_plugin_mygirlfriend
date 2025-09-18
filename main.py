import asyncio
import time
import datetime
import random
import json

from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api import AstrBotConfig, logger

# 插件注册
@register("mygirlfriend", "你的名字", "一个动态、拟人化的主动回复插件", "1.0.0")
class MyGirlfriendPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.last_active = {}
        self.is_running = True

        if self.config.get("enabled", True):
            self.background_task = asyncio.create_task(self._trigger_check_loop())
            logger.info("AI女友插件已启动，后台检查任务正在运行...")

    async def terminate(self):
        self.is_running = False
        if hasattr(self, 'background_task'):
            self.background_task.cancel()
        logger.info("AI女友插件已停止。")

    @filter.command("girlfriend_talk")
    async def manual_trigger_message(self, event: AstrMessageEvent):
        """手动触发一次主动对话(忽略休眠时间)。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员才能使用此命令哦。")
            return

        yield event.plain_result("好的，我让她现在就跟你说说话...")
        logger.info(f"管理员手动触发了对 UMO {event.unified_msg_origin} 的主动对话。")
        
        try:
            # 手动触发时，直接生成消息，忽略休眠规则
            final_message_text = await self._generate_proactive_message(event.unified_msg_origin, ignore_sleep=True)
            if final_message_text:
                yield event.plain_result(final_message_text)
            else:
                yield event.plain_result("抱歉，她现在好像不知道该说什么... (可能因为模型调用失败)")
        except Exception as e:
            logger.error(f"手动触发时出错: {e}")
            yield event.plain_result(f"糟糕，我们在沟通时遇到了一个问题: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _update_user_activity(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        umo = event.unified_msg_origin
        whitelist = self.config.get("whitelist_users", [])
        if hasattr(event, 'platform') and event.platform == 'webchat' or user_id in whitelist:
            self.last_active[umo] = time.time()
            logger.debug(f"会话 {umo} 活跃时间已更新。")

    async def _trigger_check_loop(self):
        while self.is_running:
            try:
                rules = self.config.get("rules", {})
                check_interval_seconds = rules.get("check_interval_minutes", 60) * 60
                await asyncio.sleep(check_interval_seconds)
                
                logger.debug("开始执行新一轮不活跃用户检查...")
                inactive_threshold_seconds = rules.get("inactive_hours", 24) * 3600
                current_time = time.time()
                umos_to_check = list(self.last_active.items())

                for umo, last_active_time in umos_to_check:
                    if current_time - last_active_time > inactive_threshold_seconds:
                        logger.info(f"检测到不活跃会话 {umo}，准备触发主动对话流程。")
                        asyncio.create_task(self._handle_inactive_user(umo))
                        self.last_active[umo] = current_time
            except asyncio.CancelledError:
                logger.info("后台检查任务被取消。")
                break
            except Exception as e:
                logger.error(f"后台检查任务出现错误: {e}")
                await asyncio.sleep(60)

    async def _handle_inactive_user(self, umo: str):
        """处理不活跃用户，包含智能休眠和延迟逻辑"""
        try:
            # --- 新增的智能休眠与延迟逻辑 ---
            rules = self.config.get("rules", {})
            sleep_start = rules.get("sleep_start_hour", 0)
            sleep_end = rules.get("sleep_end_hour", 10)
            now = datetime.datetime.now()
            
            # 检查当前是否处于休眠时段
            if sleep_start <= now.hour < sleep_end:
                # 计算到唤醒时间点的时长
                wake_up_time = now.replace(hour=sleep_end, minute=0, second=0, microsecond=0)
                # 如果唤醒时间点已经过去（例如在休眠结束前一刻触发），则设置为第二天的唤醒时间
                if wake_up_time < now:
                    wake_up_time += datetime.timedelta(days=1)
                
                # 在唤醒时间点后，再增加0-1小时的随机延迟
                random_delay_seconds = random.randint(0, 3600)
                scheduled_time = wake_up_time + datetime.timedelta(seconds=random_delay_seconds)
                
                sleep_duration = (scheduled_time - now).total_seconds()
                
                if sleep_duration > 0:
                    logger.info(f"当前为休眠时间，会话 {umo} 的主动对话已重新调度，将在 {sleep_duration:.0f} 秒后执行。")
                    await asyncio.sleep(sleep_duration)

            # --- 核心消息生成与发送流程 ---
            final_message_text = await self._generate_proactive_message(umo)
            if final_message_text:
                logger.info(f"后台任务为 {umo} 生成消息，尝试通过 context.send_message 推送...")
                message_to_send = MessageChain().message(final_message_text)
                await self.context.send_message(umo, message_to_send)
                logger.info(f"已成功调用 context.send_message 向会话 {umo} 发送主动消息。")
        except Exception as e:
            logger.error(f"后台任务调用LLM或发送消息时出错 (会话: {umo}): {e}")

    async def _generate_proactive_message(self, umo: str, ignore_sleep: bool = False) -> str | None:
        """通过两步走的方案，生成最终的对话文本。"""
        # 手动触发时可以忽略休眠检查
        if not ignore_sleep:
            rules = self.config.get("rules", {})
            sleep_start = rules.get("sleep_start_hour", 0)
            sleep_end = rules.get("sleep_end_hour", 10)
            if sleep_start <= datetime.datetime.now().hour < sleep_end:
                logger.info(f"消息生成被调用，但当前处于休眠时段，取消本次生成。")
                return None

        # --- 从配置中读取模型和提示词 ---
        providers_config = self.config.get("providers", {})
        prompts_config = self.config.get("prompts", {})
        huati_provider_id = providers_config.get("huati_provider_id")
        
        default_huati_prompt = "请扮演一个AI女孩，用一句话描述一件你今天发生的、独特的趣事。请直接返回趣事内容，不要任何多余的文字。"
        huati_prompt = prompts_config.get("huati_prompt", default_huati_prompt)

        # --- 第一步：话题生成 (Huati Call) ---
        if not huati_provider_id:
            logger.error("未配置话题生成模型ID (huati_provider_id)，无法继续。")
            return None
        
        try:
            huati_provider = self.context.get_provider_by_id(huati_provider_id)
            if not huati_provider:
                logger.error(f"找不到话题生成模型: {huati_provider_id}")
                return None

            logger.debug(f"为会话 {umo} 调用 Huati 模型生成话题...")
            huati_response = await huati_provider.text_chat(prompt=huati_prompt, context=None)
            todays_event = huati_response.completion_text

            if not todays_event:
                logger.warning("Huati 模型生成了空话题，取消本次主动对话。")
                return None
            logger.info(f"Huati 模型生成话题: {todays_event}")

        except Exception as e:
            logger.error(f"调用 Huati 模型时出错: {e}")
            return None

        # --- 第二步：聊天生成 (Chat Call) ---
        chat_provider_id = providers_config.get("chat_provider_id") or huati_provider_id
        try:
            chat_provider = self.context.get_provider_by_id(chat_provider_id)
            if not chat_provider:
                logger.error(f"找不到聊天生成模型: {chat_provider_id}")
                return None

            # 1. 获取真实对话历史
            history_text = "无"
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                if cid:
                    conversation = await self.context.conversation_manager.get_conversation(umo, cid)
                    if conversation and conversation.history:
                        history_list = json.loads(conversation.history)[-5:]
                        formatted_history = []
                        for msg in history_list:
                            role = "我" if msg.get('role') == 'user' else "你"
                            content = msg.get('content', '')
                            formatted_history.append(f"{role}: {content}")
                        history_text = "\n".join(formatted_history)
            except Exception as e:
                logger.error(f"为会话 {umo} 获取对话历史失败: {e}")

            # 2. 构建最终提示词
            rules = self.config.get("rules", {})
            chat_prompt_template = prompts_config.get("chat_prompt", "")
            inactive_hours = rules.get("inactive_hours", 24)
            
            chat_prompt = chat_prompt_template.format(
                inactive_hours=inactive_hours,
                todays_event=todays_event,
                history_text=history_text
            )

            logger.debug(f"为会话 {umo} 调用 Chat 模型生成最终消息...")
            chat_response = await chat_provider.text_chat(prompt=chat_prompt)
            return chat_response.completion_text

        except Exception as e:
            logger.error(f"调用 Chat 模型时出错: {e}")
            return None
