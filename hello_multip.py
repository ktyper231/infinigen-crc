import os
from multiprocessing import Process, Queue


def info(title):
    print(title)
    print("module name:", __name__)
    print("parent process:", os.getppid())
    print("process id:", os.getpid())


def f(name):
    info("function_f")
    print(f"Hello, {name}")


def fcalc(x, queue):
    info("function_fcalc")
    result = x * x
    queue.put(result)


if __name__ == "__main__":
    # auto create process
    tasks = [1, 2, 3, 4, 5]
    processes = []
    queue = Queue()

    for task in tasks:
        process = Process(target=fcalc, args=(task, queue))
        processes.append(process)
        process.start()

    for process in processes:
        process.join()

    results = []
    while not queue.empty():
        results.append(queue.get())
        print(f"result: {results[-1]}")
    print(f"results: {results}")

    # simple parallel using process
    p1 = Process(target=f, args=("bob",))
    p2 = Process(target=f, args=("miaomiao",))
    p1.start()
    p2.start()
    p1.join()
    p2.join()

    # parallel using pool
    # with Pool(5) as p:
    # 	print(p.map(fcalc, [1,2,3], queue))
