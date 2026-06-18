from kaggle_environments import make, evaluate
from agent import my_agent


def run_self_play(steps=10):
    # Create a simple environment; replace 'rps' with your environment id if different
    env = make('rps', debug=True)

    # Run a single episode with two instances of our agent (self vs self)
    result = env.run([my_agent, my_agent])

    # 'result' contains the steps; evaluate returns rewards if needed
    print('Simulation finished. Last step:')
    print(result[-1])


if __name__ == '__main__':
    run_self_play()
