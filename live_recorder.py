import asyncio
import json
import os
import re
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Dict, Tuple, Union
from urllib.parse import parse_qs

import anyio
import ffmpeg
import httpx
import jsengine
import streamlink
from httpx_socks import AsyncProxyTransport
from jsonpath_ng.ext import parse
from loguru import logger
from streamlink.options import Options
from streamlink.stream import StreamIO, HTTPStream, HLSStream
from streamlink_cli.main import open_stream
from streamlink_cli.output import FileOutput
from streamlink_cli.streamrunner import StreamRunner

from icecream import ic

recording: Dict[str, Tuple[StreamIO, FileOutput]] = {}


class LiveRecoder:
    def __init__(self, config: dict, user: dict):
        self.id = user["id"]
        
        self.name = user.get("name", "").strip()
        
        self.platform = user["platform"]
        
        self.flag = f"{self.platform} {self.name}"

        self.interval = user.get("interval", config.get("interval", 15))

        self.headers = user.get("headers", {"User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.71 Safari/537.36"})

        self.cookies = user.get("cookies",config.get("Pandalive_cookies"))

        self.proxy = user.get("proxy", config.get(f"{self.platform}_proxy", config.get("proxy")))
        
        self.format = user.get("format")

        self.output = user.get("output", config.get(f"{self.platform}_output", config.get("output", "output")))

        self.get_cookies()

        self.client = self.get_client()

    async def start(self):
        logger.info(f"{self.flag} 正在检测直播状态")
        try:
            while True:
                try:
                    await self.run()
                    await asyncio.sleep(self.interval)
                except ConnectionError as error:
                    if "直播检测请求协议错误" not in str(error):
                        logger.error(error)
                    await self.client.aclose()
                    self.client = self.get_client()
                except Exception as error:
                    logger.exception(f"{self.flag} 直播检测错误\n{repr(error)}")
        except (SystemExit, KeyboardInterrupt, asyncio.CancelledError):
            logger.info(f"{self.flag} 接收到终止信号，正在关闭")
        finally:
            await self.client.aclose()
            for url in list(recording.keys()):
                stream_fd, output = recording.pop(url)
                stream_fd.close()
                output.close()
            logger.info(f"{self.flag} 直播监控结束，资源已清理")

    async def run(self):
        pass

    async def request(self, method, url, **kwargs):
        try:
            response = await self.client.request(method, url, **kwargs)
            return response
        except httpx.ProtocolError as error:
            raise ConnectionError(f"{self.flag} 直播检测请求协议错误\n{error}")
        except httpx.HTTPError as error:
            raise ConnectionError(f"{self.flag} 直播检测请求错误\n{repr(error)}")
        except anyio.EndOfStream as error:
            raise ConnectionError(f"{self.flag} 直播检测代理错误\n{error}")

    def get_client(self):
        client_kwargs = {
            "http2": True,
            "timeout": self.interval,
            "limits": httpx.Limits(
                max_keepalive_connections=100, keepalive_expiry=self.interval * 2
            ),
            "headers": self.headers,
            "cookies": self.cookies,
        }
        # 检查是否有设置代理
        if self.proxy:
            if "socks" in self.proxy:
                client_kwargs["transport"] = AsyncProxyTransport.from_url(self.proxy)
            else:
                client_kwargs["proxies"] = self.proxy
        return httpx.AsyncClient(**client_kwargs)

    def get_cookies(self):
        if self.cookies:
            cookies = SimpleCookie()
            cookies.load(self.cookies)
            self.cookies = {k: v.value for k, v in cookies.items()}

    def get_filename(self, modelname, format):
        live_time = time.strftime("%Y-%m-%d %H%M%S")
        # 文件名特殊字符转换为全角字符
        char_dict = {
            '"': "＂",
            "*": "＊",
            ":": "：",
            "<": "＜",
            ">": "＞",
            "?": "？",
            "/": "／",
            "\\": "＼",
            "|": "｜",
        }
        for half, full in char_dict.items():
            modelname = modelname.replace(half, full)
        filename = f"{modelname} {live_time}.{format}"
        return filename

    def get_streamlink(self):
        session = streamlink.session.Streamlink(
            {"stream-segment-timeout": 60, "hls-segment-queue-threshold": 10}
        )
        # 添加streamlink的http相关选项
        if proxy := self.proxy:
            # 代理为socks5时，streamlink的代理参数需要改为socks5h，防止部分直播源获取失败
            if "socks" in proxy:
                proxy = proxy.replace("://", "h://")
            session.set_option("http-proxy", proxy)
        if self.headers:
            session.set_option("http-headers", self.headers)
        if self.cookies:
            session.set_option("http-cookies", self.cookies)
        return session

    def run_record(self, stream: Union[StreamIO, HTTPStream], url, modelname, format):
        # 如果不存在则创建，否则不创建
        if not os.path.exists(os.path.join(self.output, modelname)):
            os.makedirs(os.path.join(self.output, modelname))
        # 获取输出文件名
        filename = f"{modelname}/" + self.get_filename(modelname, format)
        # filename = self.get_filename(modelname, format)
        try:
            if stream:
                logger.info(f"{self.flag} 开始录制：{filename}")
                # 调用streamlink录制直播
                result = self.stream_writer(stream, url, filename)
                # 录制成功、format配置存在且不等于直播平台默认格式时运行ffmpeg封装
                if result and self.format and self.format != format:
                    self.run_ffmpeg(filename, format)
                logger.info(f"{self.flag} 停止录制：{filename}")
            else:
                logger.error(f"{self.flag} 无可用直播源：{filename}")
        finally:
            recording.pop(url, None)
            logger.info(f"{self.flag} 停止录制：{filename}")

    def stream_writer(self, stream, url, filename):
        logger.info(f"{self.flag} 获取到直播流链接：{filename}\n{stream.url}")
        output = FileOutput(Path(f"{self.output}/{filename}"))
        try:
            stream_fd, prebuffer = open_stream(stream)
            output.open()
            recording[url] = (stream_fd, output)
            logger.info(f"{self.flag} 正在录制：{filename}")
            StreamRunner(stream_fd, output, show_progress=True).run(prebuffer)
            return True
        except Exception as error:
            if "timeout" in str(error):
                logger.warning(
                    f"{self.flag} 直播录制超时，请检查主播是否正常开播或网络连接是否正常：{filename}\n{error}"
                )
            elif re.search(
                f"(Unable to open URL|No data returned from stream)", str(error)
            ):
                logger.warning(
                    f"{self.flag} 直播流打开错误，请检查主播是否正常开播：{filename}\n{error}"
                )
            else:
                logger.exception(f"{self.flag} 直播录制错误：{filename}\n{error}")
        finally:
            output.close()

    def run_ffmpeg(self, filename, format):
        logger.info(f"{self.flag}开始ffmpeg封装：{filename}")
        new_filename = filename.replace(f".{format}", f".{self.format}")
        ffmpeg.input(f"{self.output}/{filename}", flags="global_header").output(
            f"{self.output}/{new_filename}",
            codec="copy",
            map_metadata="-1",
            movflags="faststart",
        ).global_args("-hide_banner").run()
        os.remove(f"{self.output}/{filename}")


class Bilibili(LiveRecoder):
    async def run(self):
        url = f"https://live.bilibili.com/{self.id}"
        if url not in recording:
            response = (
                await self.request(
                    method="GET",
                    url="https://api.live.bilibili.com/room/v1/Room/get_info",
                    params={"room_id": self.id},
                )
            ).json()
            if response["data"]["live_status"] == 1:
                title = response["data"]["title"]
                stream = (
                    self.get_streamlink().streams(url).get("best")
                )  # HTTPStream[flv]
                await asyncio.to_thread(self.run_record, stream, url, title, "flv")


class Douyu(LiveRecoder):
    async def run(self):
        url = f"https://www.douyu.com/{self.id}"
        if url not in recording:
            response = (
                await self.request(
                    method="GET",
                    url=f"https://open.douyucdn.cn/api/RoomApi/room/{self.id}",
                )
            ).json()
            if response["data"]["room_status"] == "1":
                modelname = response["data"]["owner_name"]
                if self.name:
                    modelname = self.name
                stream = HTTPStream(
                    self.get_streamlink(), await self.get_live()
                )  # HTTPStream[flv]
                await asyncio.to_thread(self.run_record, stream, url, modelname, "flv")

    async def get_js(self):
        response = (
            await self.request(
                method="POST",
                url=f"https://www.douyu.com/swf_api/homeH5Enc?rids={self.id}",
            )
        ).json()
        js_enc = response["data"][f"room{self.id}"]
        crypto_js = (
            await self.request(
                method="GET",
                url="https://cdn.staticfile.org/crypto-js/4.1.1/crypto-js.min.js",
            )
        ).text
        return jsengine.JSEngine(js_enc + crypto_js)

    async def get_live(self):
        did = uuid.uuid4().hex
        tt = str(int(time.time()))
        params = {"cdn": "tct-h5", "did": did, "tt": tt, "rate": 0}
        js = await self.get_js()
        query = js.call("ub98484234", self.id, did, tt)
        params.update({k: v[0] for k, v in parse_qs(query).items()})
        response = (
            await self.request(
                method="POST",
                url=f"https://www.douyu.com/lapi/live/getH5Play/{self.id}",
                params=params,
            )
        ).json()
        return f"{response['data']['rtmp_url']}/{response['data']['rtmp_live']}"


class Huya(LiveRecoder):
    async def run(self):
        url = f"https://www.huya.com/{self.id}"
        if url not in recording:
            response = (await self.request(method="GET", url=url)).text
            if '"isOn":true' in response:
                title = re.search('"introduction":"(.*?)"', response).group(1)
                stream = (
                    self.get_streamlink().streams(url).get("best")
                )  # HTTPStream[flv]
                await asyncio.to_thread(self.run_record, stream, url, title, "flv")


class Douyin(LiveRecoder):
    async def run(self):
        url = f"https://live.douyin.com/{self.id}"
        if url not in recording:
            if not self.client.cookies:
                await self.client.get(url="https://live.douyin.com/")  # 获取ttwid
            response = (
                await self.request(
                    method="GET",
                    url="https://live.douyin.com/webcast/room/web/enter/",
                    params={
                        "aid": 6383,
                        "device_platform": "web",
                        "browser_language": "zh-CN",
                        "browser_platform": "Win32",
                        "browser_name": "Chrome",
                        "browser_version": "100.0.0.0",
                        "web_rid": self.id,
                    },
                )
            ).json()
            if data := response["data"]["data"]:
                data = data[0]
                if data['status'] == 2:
                    title = data['title']
                    live_url = ''
                    stream_data = json.loads(data['stream_url']['live_core_sdk_data']['pull_data']['stream_data'])
                    for quality_code in ('origin', 'uhd', 'hd', 'sd', 'md', 'ld'):
                        if quality_data := stream_data['data'].get(quality_code):
                            live_url = quality_data['main']['flv']
                            break
                    stream = HTTPStream(
                        self.get_streamlink(),
                        live_url
                    )  # HTTPStream[flv]
                    await asyncio.to_thread(self.run_record, stream, url, title, "flv")


class Youtube(LiveRecoder):
    async def run(self):
        response = (
            await self.request(
                method="POST",
                url="https://www.youtube.com/youtubei/v1/browse",
                params={
                    "key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
                    "prettyPrint": False,
                },
                json={
                    "context": {
                        "client": {
                            "hl": "zh-CN",
                            "clientName": "MWEB",
                            "clientVersion": "2.20230101.00.00",
                            "timeZone": "Asia/Shanghai",
                        }
                    },
                    "browseId": self.id,
                    "params": "EgdzdHJlYW1z8gYECgJ6AA%3D%3D",
                },
            )
        ).json()
        jsonpath = parse("$..videoWithContextRenderer").find(response)
        for match in jsonpath:
            video = match.value
            if '"style": "LIVE"' in json.dumps(video):
                url = f"https://www.youtube.com/watch?v={video['videoId']}"
                title = video["headline"]["runs"][0]["text"]
                if url not in recording:
                    stream = (
                        self.get_streamlink().streams(url).get("best")
                    )  # HLSStream[mpegts]
                    # FIXME:多开直播间中断
                    asyncio.create_task(
                        asyncio.to_thread(self.run_record, stream, url, title, "ts")
                    )


class Twitch(LiveRecoder):
    async def run(self):
        url = f"https://www.twitch.tv/{self.id}"
        if url not in recording:
            response = (
                await self.request(
                    method="POST",
                    url="https://gql.twitch.tv/gql",
                    headers={"Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko"},
                    json=[
                        {
                            "operationName": "StreamMetadata",
                            "variables": {"channelLogin": self.id},
                            "extensions": {
                                "persistedQuery": {
                                    "version": 1,
                                    "sha256Hash": "a647c2a13599e5991e175155f798ca7f1ecddde73f7f341f39009c14dbf59962",
                                }
                            },
                        }
                    ],
                )
            ).json()
            if response[0]["data"]["user"]["stream"]:
                modelname = self.id
                if self.name:
                    modelname = self.name
                options = Options()
                options.set("disable-ads", True)
                stream = (
                    self.get_streamlink().streams(url, options).get("best")
                )  # HLSStream[mpegts]
                await asyncio.to_thread(self.run_record, stream, url, modelname, "ts")


class Niconico(LiveRecoder):
    async def run(self):
        url = f"https://live.nicovideo.jp/watch/{self.id}"
        if url not in recording:
            response = (await self.request(method="GET", url=url)).text
            if '"content_status":"ON_AIR"' in response:
                title = json.loads(
                    re.search(
                        r'<script type="application/ld\+json">(.*?)</script>', response
                    ).group(1)
                )["name"]
                stream = (
                    self.get_streamlink().streams(url).get("best")
                )  # HLSStream[mpegts]
                await asyncio.to_thread(self.run_record, stream, url, title, "ts")


class Twitcasting(LiveRecoder):
    async def run(self):
        url = f"https://twitcasting.tv/{self.id}"
        if url not in recording:
            response = (
                await self.request(
                    method="GET",
                    url="https://twitcasting.tv/streamserver.php",
                    params={"target": self.id, "mode": "client"},
                )
            ).json()
            if response:
                response = (await self.request(method="GET", url=url)).text
                title = re.search(
                    '<meta name="twitter:title" content="(.*?)">', response
                ).group(1)
                stream = self.get_streamlink().streams(url).get("best")  # Stream[mp4]
                await asyncio.to_thread(self.run_record, stream, url, title, "mp4")


class Afreeca(LiveRecoder):
    async def run(self):
        url = f"https://play.afreecatv.com/{self.id}"
        if url not in recording:
            response = (
                await self.request(
                    method="POST",
                    url="https://live.afreecatv.com/afreeca/player_live_api.php",
                    data={"bid": self.id},
                )
            ).json()
            # ic(response)
            if response["CHANNEL"]["RESULT"] != 0:
                modelname = self.id
                if self.name:
                    modelname = self.name
                stream = (
                    self.get_streamlink().streams(url).get("best")
                )  # HLSStream[mpegts]
                await asyncio.to_thread(self.run_record, stream, url, modelname, "ts")


class Pandalive(LiveRecoder):
    async def run(self):
        url = f"https://www.pandalive.co.kr/live/play/{self.id}"
        if url not in recording:
            response = (
                await self.request(
                    method="POST",
                    url="https://api.pandalive.co.kr/v1/live/play",
                    headers={"x-device-info": '{"t":"webMobile","v":"1.0","ui":0}'},
                    data={"action": "watch", "userId": self.id},
                )
            ).json()
            if response["result"]:
                modelname = (
                    f"{response['media']['userNick']} ({response['media']['userId']})"
                )
                if self.name:
                    modelname = self.name
                stream = (
                    self.get_streamlink().streams(url).get("best")
                )  # HLSStream[mpegts]
                await asyncio.to_thread(self.run_record, stream, url, modelname, "ts")


class Bigolive(LiveRecoder):
    async def run(self):
        url = f"https://www.bigo.tv/cn/{self.id}"
        if url not in recording:
            response = (
                await self.request(
                    method="POST",
                    url="https://ta.bigo.tv/official_website/studio/getInternalStudioInfo",
                    params={"siteId": self.id},
                )
            ).json()
            if response["data"]["alive"]:
                clientBigoId = response["data"]["clientBigoId"]
                country_code = response["data"]["country_code"]
                modelname = f"{country_code}_{clientBigoId}"
                if self.name:
                    modelname = self.name
                stream = HLSStream(
                    session=self.get_streamlink(), url=response["data"]["hls_src"]
                )  # HLSStream[mpegts]
                await asyncio.to_thread(self.run_record, stream, url, modelname, "ts")


class Pixivsketch(LiveRecoder):
    async def run(self):
        url = f'https://sketch.pixiv.net/{self.id}'
        if url not in recording:
            response = (await self.request(
                method='GET',
                url=url
            )).text
            next_data = json.loads(re.search(r'<script id="__NEXT_DATA__".*?>(.*?)</script>', response)[1])
            initial_state = json.loads(next_data['props']['pageProps']['initialState'])
            if lives := initial_state['live']['lives']:
                live = list(lives.values())[0]
                title = live['name']
                streams = HLSStream.parse_variant_playlist(
                    session=self.get_streamlink(),
                    url=live['owner']['hls_movie']
                )
                stream = list(streams.values())[0]  # HLSStream[mpegts]
                await asyncio.to_thread(self.run_record, stream, url, title, 'ts')


class Chaturbate(LiveRecoder):
    async def run(self):
        url = f'https://chaturbate.com/{self.id}'
        if url not in recording:
            try:
                response = (await self.request(
                    method='POST',
                    url='https://chaturbate.com/get_edge_hls_url_ajax/',
                    headers={
                    'X-Requested-With': 'XMLHttpRequest'
                    },
                    data={
                        'room_slug': self.id
                        }
                )).json()
            except Exception as e:
                response = None

            if response and response['room_status'] == 'public':
                modelname = self.id
                if self.name:
                    modelname = self.name
                streams = HLSStream.parse_variant_playlist(
                    session=self.get_streamlink(),
                    url=response['url']
                )
                stream = list(streams.values())[2]
                await asyncio.to_thread(self.run_record, stream, url, modelname, 'ts')


class Stripchat(LiveRecoder):
    async def run(self):
        url = f'https://stripchat.com/{self.id}'  # 构建直播间的URL
        if url not in recording:
            api_url = f"https://stripchat.com/api/front/v2/models/username/{self.id}/cam"  # 构建API请求的URL
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": url,
            }

            response = (await self.request(
                method='GET',
                url=api_url,
                headers=headers
            )).json()  # 发送GET请求并解析JSON响应

            if not response:
                # logger.info("Not a valid url.")  # 如果响应为空，记录日志并返回
                return

            self.author = self.id  # 设置作者为直播间ID
            self.title = response["cam"]["topic"]  # 设置标题为直播间主题

            # 构建不同的流媒体服务器URL
            server = f"https://b-{response['cam']['viewServers']['flashphoner-hls']}.doppiocdn.com/hls/{response['cam']['streamName']}/master/{response['cam']['streamName']}_auto.m3u8?playlistType=standard"
            server_src = f"https://b-{response['cam']['viewServers']['flashphoner-hls']}.doppiocdn.com/hls/{response['cam']['streamName']}/master/{response['cam']['streamName']}.m3u8?playlistType=standard"
            server0 = f"https://edge-hls.doppiocdn.com/hls/{response['cam']['streamName']}/master/{response['cam']['streamName']}_auto.m3u8?playlistType=standard"
            server1 = f"https://edge-hls.doppiocdn.org/hls/{response['cam']['streamName']}/master/{response['cam']['streamName']}_auto.m3u8?playlistType=standard"
            server2 = f"https://b-{response['cam']['viewServers']['flashphoner-hls']}.doppiocdn.org/hls/{response['cam']['streamName']}/{response['cam']['streamName']}.m3u8"

            # logger.info(f"Stream status: {response['user']['user']['status']}")  # 记录流媒体状态

            if response["user"]["user"]["isLive"] and response["user"]["user"]["status"] == "public" and server:
                try:
                    logger.info(f"trying server {server}")  # 尝试第一个服务器
                    streams = HLSStream.parse_variant_playlist(self.get_streamlink(), server, headers={'Referer': url})
                    for s in streams.items():
                        await asyncio.to_thread(self.run_record, s[1], url, self.id, 'ts')
                    streams_src = HLSStream.parse_variant_playlist(self.get_streamlink(), server_src, headers={'Referer': url})
                    for s in streams_src.items():
                        await asyncio.to_thread(self.run_record, s[1], url, self.id, 'ts')
                except IOError as err:
                    try:
                        logger.info(f"trying fallback server {server0}")  # 尝试备用服务器0
                        streams0 = HLSStream.parse_variant_playlist(self.get_streamlink(), server0, headers={'Referer': url})
                        for s in streams0.items():
                            await asyncio.to_thread(self.run_record, s[1], url, self.id, 'ts')
                    except IOError as err:
                        try:
                            logger.info(f"trying another fallback server {server1}")  # 尝试备用服务器1
                            streams1 = HLSStream.parse_variant_playlist(self.get_streamlink(), server1, headers={'Referer': url})
                            for s in streams1.items():
                                await asyncio.to_thread(self.run_record, s[1], url, self.id, 'ts')
                        except IOError as err:
                            logger.info(f"fallback to default stream : {server2}")  # 尝试备用服务器2
                            stream = HLSStream(self.get_streamlink(), server2)
                            await asyncio.to_thread(self.run_record, stream, url, self.id, 'ts')


async def run(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    try:
        tasks = []
        for item in config["user"]:
            platform_class = globals()[item["platform"]]
            coro = platform_class(config, item).start()
            tasks.append(asyncio.create_task(coro))
        await asyncio.wait(tasks)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        logger.warning("用户中断录制，正在关闭直播流")
        for stream_fd, output in recording.copy().values():
            stream_fd.close()
            output.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python live_recorder.py <config_file>")
        sys.exit(1)
    config_path = sys.argv[1]
    logger.add(
        sink="logs/log_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="3 days",
        level="INFO",
        encoding="utf-8",
        format="[{time:YYYY-MM-DD HH:mm:ss}][{level}][{name}][{function}:{line}]{message}",
    )
    asyncio.run(run(config_path))
