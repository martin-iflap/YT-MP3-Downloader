// Create context menu when extension is installed
browser.runtime.onInstalled.addListener(() => {
  console.log('YouTube MP3 Downloader installed');

  // Create right-click context menu for YouTube links
  browser.contextMenus.create({
    id: "download-mp3",
    title: "Download as MP3",
    contexts: ["link"],
    targetUrlPatterns: [
      "*://www.youtube.com/watch*",
      "*://youtube.com/watch*",
      "*://youtu.be/*",
      "*://m.youtube.com/watch*",
      "*://www.youtube.com/shorts/*",
      "*://youtube.com/shorts/*"
    ]
  });
});

// Handle context menu clicks
browser.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === "download-mp3") {
    const url = info.linkUrl;
    console.log('Context menu download initiated for:', url);

    // Validate it's a YouTube URL
    if (isYouTubeUrl(url)) {
      downloadMP3(url);
    } else {
      showNotification('Invalid URL', 'Not a valid YouTube URL', 'error');
    }
  }
});

// Check if URL is a valid YouTube URL
function isYouTubeUrl(url) {
  return url && (
    url.includes('youtube.com/watch') ||
    url.includes('youtu.be/') ||
    url.includes('youtube.com/shorts/')
  );
}

// Download MP3 from YouTube URL
function downloadMP3(url) {
  console.log('Starting download for:', url);

  // Show initial notification
  showNotification('Download Started', 'Downloading MP3...', 'info');

  try {
    // Connect to native messaging host
    const port = browser.runtime.connectNative('app_manifest');

    // Send download request
    port.postMessage({ url: url });

    // Handle response from native app
    port.onMessage.addListener((response) => {
      console.log('Received response from native app:', response);

      if (response.status === 'success') {
        showNotification(
          'Download Complete',
          `${response.filename || 'YouTube MP3'} saved successfully!`,
          'success'
        );
      } else if (response.status === 'error') {
        showNotification(
          'Download Failed',
          response.message || 'An error occurred during download',
          'error'
        );
      } else if (response.status === 'warning') {
        // Show the specific warning message
        showNotification(
          'Warning',
          response.message || 'Download completed with warnings',
          'warning'
        );
      }
    });

    // Handle disconnection errors
    port.onDisconnect.addListener(() => {
      const error = browser.runtime.lastError;

      if (error) {
        console.error('Native messaging error:', error);
        showNotification(
          'Connection Error',
          'Failed to connect to native app. Make sure it is properly installed.',
          'error'
        );
      }
    });

  } catch (error) {
    console.error('Error initiating download:', error);
    showNotification(
      'Error',
      `Failed to start download: ${error.message}`,
      'error'
    );
  }
}

// Show browser notification
function showNotification(title, message, type) {
  const iconMap = {
    success: 'icons/icon-48.png',
    error: 'icons/icon-48.png',
    warning: 'icons/icon-48.png',
    info: 'icons/icon-48.png'
  };

  browser.notifications.create({
    type: 'basic',
    iconUrl: iconMap[type] || 'icons/icon-48.png',
    title: title,
    message: message
  });
}