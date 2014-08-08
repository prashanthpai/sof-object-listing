Enable kernel debug tracing

~~~
echo 1 > /sys/kernel/debug/tracing/tracing_on
~~~

Enable tracing of all inotify syscalls
~~~
for i in `ls /sys/kernel/debug/tracing/events/syscalls | grep inotify`; do echo 1 >| /sys/kernel/debug/tracing/events/syscalls/$i/enable; done;
~~~

Check trace output
~~~
cat /sys/kernel/debug/tracing/trace
~~~