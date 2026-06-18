import random


def my_agent(observation, configuration):
    """A minimal example agent for kaggle-environments.

    Returns a random action. Adjust the action format to your specific environment.
    """
    # Example actions: integer move index, or dict/str depending on environment
    # Here we return a random integer between 0 and 2 as a placeholder
    return random.randint(0, 2)
