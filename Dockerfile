FROM whyour/qinglong:latest
# 设置时区
ENV TZ=Asia/Shanghai
# 开放面板端口
EXPOSE 5700
# 持久化数据目录
VOLUME ["/ql/data"]
# 容器启动命令（青龙内置进程管理）
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
