"""FizzBuzz: 3 的倍数 Fizz, 5 的倍数 Buzz, 公倍数 FizzBuzz."""

for i in range(1, 16):
    if i % 15 == 0:
        print("FizzBuzz")
    elif i % 3 == 0:
        print("Fizz")
    elif i % 5 == 0:
        print("Buzz")
    else:
        print(i)
