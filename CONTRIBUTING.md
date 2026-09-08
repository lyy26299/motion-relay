# Contributing to Vision Agent Real Demo

First off, thank you for considering contributing to Vision Agent Real Demo! It's people like you that make this project such a great tool.

## Code of Conduct

This project and everyone participating in it is governed by our Code of Conduct. By participating, you are expected to uphold this code.

## How Can I Contribute?

### Reporting Bugs

Before creating bug reports, please check the existing issues to avoid duplicates. When you create a bug report, include as many details as possible:

**Bug Report Template**:
- Vision-Agents version
- Python version
- Operating system
- Steps to reproduce
- Expected behavior
- Actual behavior
- Error messages (if any)
- Screenshots (if applicable)

### Suggesting Enhancements

Enhancement suggestions are tracked as GitHub issues. When creating an enhancement suggestion, please include:

- Clear and descriptive title
- Detailed description of the proposed functionality
- Why this enhancement would be useful
- Examples of how it would work

### Pull Requests

1. **Fork the repo** and create your branch from `main`
2. **Make your changes**:
   - If you've added code, add tests
   - If you've changed APIs, update the documentation
   - Ensure the test suite passes
   - Make sure your code lints
3. **Commit your changes**:
   - Use clear and meaningful commit messages
   - Follow conventional commits format (optional but appreciated)
4. **Push to your fork** and submit a pull request
5. **Wait for review** - maintainers will review your PR

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/vision-agent-real-demo.git
cd vision-agent-real-demo

# Install dependencies
uv sync

# Set up API keys
cp .env.example .env
# Fill in your API keys

# Run tests
uv run python tests/test_setup.py

# Start the agent
./scripts/run.sh
```

## Coding Standards

### Python Style Guide

- Follow [PEP 8](https://www.python.org/dev/peps/pep-0008/)
- Use type hints where possible
- Write docstrings for functions and classes
- Keep functions small and focused
- Use meaningful variable names

### Example

```python
async def create_agent(**kwargs) -> Agent:
    """
    Create a Vision Agent instance.

    Args:
        **kwargs: Additional configuration options

    Returns:
        Agent: Configured Vision Agent instance

    Raises:
        ValueError: If required configuration is missing
    """
    logger.info("Initializing Vision Agent...")
    # Implementation
```

### Documentation

- Update README.md if you change functionality
- Add docstrings to new functions
- Update docs/ if you add new features
- Include code examples where helpful

### Testing

- Write tests for new features
- Ensure existing tests pass
- Test on multiple platforms if possible (macOS, Linux, Windows)

## Project Structure

```
vision-agent-real-demo/
├── vision_agent_demo.py      # Main entry point
├── vision_assistant.md        # AI instructions
├── docs/                      # Documentation
│   ├── README_CN.md
│   ├── QUICKSTART.md
│   └── TROUBLESHOOTING.md
├── scripts/                   # Utility scripts
│   ├── run.sh
│   ├── run.bat
│   └── setup.sh
└── tests/                     # Test files
    └── test_setup.py
```

## Areas for Contribution

### High Priority

- [ ] **AR Visualization** - Draw skeleton overlay on video
- [ ] **Progress Tracking** - Save workout history
- [ ] **Custom Plans** - User-defined training programs
- [ ] **Mobile App** - iOS/Android support

### Medium Priority

- [ ] **More Exercises** - Burpees, pull-ups, yoga poses
- [ ] **Multi-language** - i18n support
- [ ] **Analytics** - Advanced workout analytics
- [ ] **Social Features** - Share workouts

### Low Priority

- [ ] **Themes** - Custom UI themes
- [ ] **Voice Commands** - Control via voice
- [ ] **Music Integration** - Workout playlists
- [ ] **Wearables** - Integrate with fitness trackers

## Adding New Exercises

To add a new exercise:

1. **Update `vision_assistant.md`**:
   ```markdown
   ## New Exercise Name

   ### Standard Form
   - Starting position
   - Movement description
   - Key points

   ### Common Mistakes
   - Error 1: Description
   - Error 2: Description

   ### Real-time Feedback
   - Before: "..."
   - During: "..."
   - After: "..."
   ```

2. **Test with YOLO**:
   - Ensure keypoints are detected correctly
   - Verify angle calculations work
   - Test error detection logic

3. **Update Documentation**:
   - Add to README.md
   - Update exercise list
   - Add usage examples

## Commit Message Guidelines

We loosely follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks

**Examples**:
```
feat(exercises): add burpee exercise support

- Added burpee instructions to vision_assistant.md
- Implemented jump detection logic
- Added tests for burpee form validation

Closes #123
```

```
fix(yolo): correct keypoint detection for tall users

The knee-ankle alignment check was failing for users over 6'2".
Updated the threshold calculations to account for different body proportions.

Fixes #45
```

## Review Process

1. **Automated Checks**: CI/CD will run tests automatically
2. **Code Review**: A maintainer will review your code
3. **Feedback**: Address any requested changes
4. **Approval**: Once approved, we'll merge your PR
5. **Release**: Changes will be included in the next release

## Getting Help

- **GitHub Issues**: For bugs and feature requests
- **GitHub Discussions**: For questions and discussions
- **Email**: support@minddock.com

## Recognition

Contributors will be:
- Listed in the README
- Mentioned in release notes
- Added to CONTRIBUTORS.md (if desired)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

Thank you for contributing! 🎉
